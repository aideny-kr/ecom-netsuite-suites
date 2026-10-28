"""Real login, commits, separate sessions and crash evidence; SDK is synthetic."""

import asyncio
import json
import os
import signal
import sys
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.core.database import set_tenant_context
from app.core.encryption import encrypt_credentials
from app.models.connection import Connection
from app.models.user import User
from app.models.workspace import (
    Workspace,
    WorkspaceArtifact,
    WorkspaceChangeSet,
    WorkspaceFile,
    WorkspacePatch,
    WorkspaceRun,
)
from app.services import runner_service
from app.services.workspace import sdk_validation_attempt as attempts
from tests.services.workspace.test_sdk_validation_attempt import synthetic_receipt
from tests.test_dedicated_runtime import installation  # noqa: F401
from tests.workspace_identity import make_target


async def seed(factory, company):
    async with factory() as db:
        await set_tenant_context(db, str(company))
        assert tuple(
            (await db.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()
        ) == (False, False)
        user = await db.scalar(select(User).where(User.tenant_id == company))
        connection, credentials, _ = await make_target(db, company)
        ws = Workspace(tenant_id=company, name="Synthetic durable validation", status="active", created_by=user.id)
        db.add(ws)
        await db.flush()
        cs = WorkspaceChangeSet(
            tenant_id=company,
            workspace_id=ws.id,
            title="Reviewed synthetic bytes",
            status="approved",
            proposed_by=user.id,
        )
        db.add(cs)
        await db.flush()
        file = WorkspaceFile(
            tenant_id=company,
            workspace_id=ws.id,
            path="SuiteScripts/a.js",
            file_name="a.js",
            content="// inert\n",
            is_directory=False,
        )
        db.add_all(
            [
                file,
                WorkspacePatch(
                    tenant_id=company,
                    changeset_id=cs.id,
                    file_path="SuiteScripts/new.js",
                    operation="create",
                    new_content="// reviewed synthetic delta\n",
                    baseline_sha256="",
                    apply_order=0,
                ),
            ]
        )
        await db.flush()
        run = await runner_service.create_run(db, company, ws.id, "suitecloud_validate", user.id, changeset_id=cs.id)
        await db.commit()
        return dict(tenant_id=company, run_id=run.id, connection_id=connection.id), file.id, credentials


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["verified", "rotated", "source", "cancel"])
async def test_real_commit_exclusive_nonce_shortlocks_and_current_state(installation, monkeypatch, mode):  # noqa: F811
    monkeypatch.setattr(settings, "DEDICATED_RUNTIME", True)
    factory = async_sessionmaker(installation["engine"], expire_on_commit=False)
    args, file_id, credentials = await seed(factory, installation["company"])
    runtime = attempts.ValidationRuntime("sha256:" + "a" * 64, "/tmp/synthetic.sock", "/usr/bin/docker", b"r" * 32)
    seen = []

    async def broker(**kwargs):
        receipt = synthetic_receipt(kwargs)
        # This new connection cannot see an uncommitted claim. Row locks must
        # already be released, including the connection's shared identity lock.
        async with factory() as observer:
            await set_tenant_context(observer, str(args["tenant_id"]))
            await observer.execute(text("SET LOCAL lock_timeout = '1000ms'"))
            run = await observer.get(WorkspaceRun, args["run_id"])
            assert run.status == "running", "claim commit not visible"
            record = await observer.scalar(
                select(WorkspaceArtifact).where(
                    WorkspaceArtifact.run_id == args["run_id"], WorkspaceArtifact.artifact_type == attempts.ATTEMPT
                )
            )
            assert record is not None, "nonce commit not visible"
            assert json.loads(record.content)["attempt_nonce"] == kwargs["attempt_nonce"]
            seen.append(record.id)
            with pytest.raises(attempts.ValidationAttemptError, match="claimed"):
                await attempts.execute_validation_attempt(observer, **args, runtime=runtime)
            connection = await observer.scalar(
                select(Connection).where(Connection.id == args["connection_id"]).with_for_update()
            )
            if mode == "rotated":
                credentials["suitecloud_ci"]["certificate_id"] = "rotated-during-execution"
                connection.encrypted_credentials = encrypt_credentials(credentials)
            if mode == "source":
                await observer.execute(
                    update(WorkspaceFile).where(WorkspaceFile.id == file_id).values(content="// concurrently edited\n")
                )
            await observer.commit()
        if mode == "cancel":
            raise asyncio.CancelledError()
        return receipt

    with patch.object(attempts.credential_executor, "validate_project", AsyncMock(side_effect=broker)) as call:
        async with factory() as db:
            await set_tenant_context(db, str(args["tenant_id"]))
            async with asyncio.timeout(10):
                if mode == "cancel":
                    with pytest.raises(asyncio.CancelledError):
                        await attempts.execute_validation_attempt(db, **args, runtime=runtime)
                else:
                    run = await attempts.execute_validation_attempt(db, **args, runtime=runtime)
                    assert run.status == ("passed" if mode == "verified" else "failed")
        async with factory() as reloaded:
            await set_tenant_context(reloaded, str(args["tenant_id"]))
            run = await reloaded.get(WorkspaceRun, args["run_id"])
            if mode == "verified":
                result = await attempts.read_validation_receipt(
                    reloaded, tenant_id=args["tenant_id"], run_id=run.id, runtime=runtime
                )
                assert result["server_validation"] is True and run.gate_status == "block"
                artifacts = (
                    await reloaded.scalars(select(WorkspaceArtifact).where(WorkspaceArtifact.run_id == run.id))
                ).all()
                assert {a.artifact_type for a in artifacts} == {attempts.ATTEMPT, attempts.RECEIPT, attempts.OUTCOME}
                assert "PRIVATE KEY" not in str([a.content for a in artifacts])
            else:
                assert run.status == ("running" if mode == "cancel" else "failed")
                with pytest.raises(attempts.AuthSeederError):
                    await attempts.read_validation_receipt(
                        reloaded, tenant_id=args["tenant_id"], run_id=run.id, runtime=runtime
                    )
            with pytest.raises(attempts.ValidationAttemptError, match="claimed"):
                await attempts.execute_validation_attempt(reloaded, **args, runtime=runtime)
        assert call.await_count == 1 and len(seen) == 1


@pytest.mark.asyncio
async def test_sigkill_after_committed_claim_survives_without_replay(installation, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "DEDICATED_RUNTIME", True)
    factory = async_sessionmaker(installation["engine"], expire_on_commit=False)
    args, _, _ = await seed(factory, installation["company"])
    env = dict(
        os.environ,
        SDK_ATTEMPT_IDS=json.dumps({k: str(v) for k, v in args.items()}),
        DATABASE_URL=installation["url"].render_as_string(hide_password=False),
        DATABASE_URL_DIRECT="",
        ENCRYPTION_KEY=settings.ENCRYPTION_KEY,
        DEDICATED_RUNTIME="false",
    )
    script = """
import asyncio, json, os, signal, uuid
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.core.database import set_tenant_context
from app.core.config import settings
from app.services.workspace import sdk_validation_attempt as a
async def crash(**kwargs): os.kill(os.getpid(), signal.SIGKILL)
async def main():
    a.credential_executor.validate_project=crash
    engine=create_async_engine(settings.DATABASE_URL)
    async with async_sessionmaker(engine,expire_on_commit=False)() as db:
        args={k:uuid.UUID(v) for k,v in json.loads(os.environ["SDK_ATTEMPT_IDS"]).items()}
        await set_tenant_context(db,str(args["tenant_id"]))
        await a.execute_validation_attempt(db,**args,runtime=a.ValidationRuntime("sha256:"+"a"*64,"/tmp/synthetic.sock","/usr/bin/docker",b"r"*32))
asyncio.run(main())
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        async with asyncio.timeout(15):
            stdout, stderr = await child.communicate()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    assert child.returncode == -signal.SIGKILL, (stdout.decode()[-500:], stderr.decode()[-500:])
    runtime = attempts.ValidationRuntime("sha256:" + "a" * 64, "/tmp/synthetic.sock", "/usr/bin/docker", b"r" * 32)
    with patch.object(attempts.credential_executor, "validate_project", AsyncMock()) as call:
        async with factory() as db:
            await set_tenant_context(db, str(args["tenant_id"]))
            run = await db.get(WorkspaceRun, args["run_id"])
            assert run.status == "running"
            assert await db.scalar(
                select(WorkspaceArtifact.id).where(
                    WorkspaceArtifact.run_id == run.id, WorkspaceArtifact.artifact_type == attempts.ATTEMPT
                )
            )
            with pytest.raises(attempts.ValidationAttemptError, match="claimed"):
                await attempts.execute_validation_attempt(db, **args, runtime=runtime)
        call.assert_not_awaited()
