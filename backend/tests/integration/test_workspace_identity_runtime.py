"""Actual restricted role, real commit/reload and rotation serialization; synthetic only."""

import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.core.database import set_tenant_context
from app.core.encryption import encrypt_credentials
from app.models.connection import Connection
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceChangeSet, WorkspaceDeployToken, WorkspaceFile
from app.services import runner_service
from app.services.deploy_preview_service import (
    DeployGateNotMetError,
    build_deploy_preview,
    mint_deploy_token,
    verify_and_consume_deploy_token,
)
from app.services.workspace.suitecloud_auth_seeder import SeededCredentials
from app.services.workspace.target_identity import require_current_target
from tests.test_dedicated_runtime import installation  # noqa: F401 -- pytest fixture
from tests.workspace_identity import make_target


@pytest.mark.asyncio
async def test_real_commits_reject_current_certificate_rotation(installation, monkeypatch):  # noqa: F811
    i = installation
    monkeypatch.setattr(settings, "DEDICATED_RUNTIME", True)
    factory = async_sessionmaker(i["engine"], expire_on_commit=False)
    async with factory() as db:
        await set_tenant_context(db, str(i["company"]))
        assert tuple(
            (await db.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()
        ) == (False, False)
        user = await db.scalar(select(User).where(User.tenant_id == i["company"]))
        connection, credentials, identity = await make_target(db, i["company"])
        ws = Workspace(
            tenant_id=i["company"], name="Synthetic current identity proof", status="active", created_by=user.id
        )
        db.add(ws)
        await db.flush()
        cs = WorkspaceChangeSet(
            tenant_id=i["company"],
            workspace_id=ws.id,
            title="Current certificate proof",
            status="approved",
            proposed_by=user.id,
        )
        db.add_all(
            [
                cs,
                WorkspaceFile(
                    tenant_id=i["company"],
                    workspace_id=ws.id,
                    path="SuiteScripts/inert.js",
                    file_name="inert.js",
                    content="// inert\n",
                    is_directory=False,
                ),
            ]
        )
        await db.commit()
        for kind in ("suitecloud_validate", "jest_unit_test"):
            await set_tenant_context(db, str(i["company"]))
            run = await runner_service.create_run(db, i["company"], ws.id, kind, user.id, changeset_id=cs.id)
            with (
                patch.object(runner_service, "_run_subprocess", AsyncMock(return_value=(0, "", ""))),
                patch(
                    "app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run",
                    AsyncMock(return_value=SeededCredentials(Path("/tmp/unused"), **identity)),
                ),
            ):
                await runner_service.execute_run(db, run.id, i["company"])
            assert run.status == "passed"
            await db.commit()
        await set_tenant_context(db, str(i["company"]))
        p = await build_deploy_preview(
            db=db,
            changeset_id=cs.id,
            sandbox_id="123-sb1",
            require_assertions=False,
            tenant_id=i["company"],
            actor_id=user.id,
        )
        minted = await mint_deploy_token(db=db, preview=p)
        await db.commit()
    async with factory() as db:
        await set_tenant_context(db, str(i["company"]))
        token = await db.get(WorkspaceDeployToken, uuid.UUID(minted["jti"]))
        assert token.target_identity == identity
        conn = await db.get(Connection, uuid.UUID(identity["connection_id"]))
        credentials["suitecloud_ci"]["certificate_id"] = "rotated"
        conn.encrypted_credentials = encrypt_credentials(credentials)
        await db.commit()
    async with factory() as db:
        await set_tenant_context(db, str(i["company"]))
        with pytest.raises(DeployGateNotMetError):
            await verify_and_consume_deploy_token(
                db=db,
                jti=uuid.UUID(minted["jti"]),
                confirmation_token=minted["confirmation_token"],
                actor_id=user.id,
                tenant_id=i["company"],
            )
        await db.commit()
    async with factory() as db:
        await set_tenant_context(db, str(i["company"]))
        token = await db.get(WorkspaceDeployToken, uuid.UUID(minted["jti"]))
        assert token.target_identity == identity and token.consumed_at is None
    print("REAL_COMMIT_RELOAD NOSUPERUSER NOBYPASSRLS full identity persisted; current certificate rotation rejected")


@pytest.mark.asyncio
async def test_current_read_refreshes_cache_and_blocks_rotation_until_commit(installation, monkeypatch):  # noqa: F811
    i = installation
    monkeypatch.setattr(settings, "DEDICATED_RUNTIME", True)
    factory = async_sessionmaker(i["engine"], expire_on_commit=False)
    async with factory() as first:
        await set_tenant_context(first, str(i["company"]))
        connection, credentials, identity = await make_target(first, i["company"])
        await first.commit()
        await set_tenant_context(first, str(i["company"]))
        await require_current_target(first, i["company"], identity)
        started = asyncio.Event()

        async def rotate():
            async with factory() as other:
                await set_tenant_context(other, str(i["company"]))
                await other.execute(text("SET LOCAL lock_timeout = '5s'"))
                started.set()
                await other.execute(text("UPDATE connections SET status='revoked' WHERE id=:id"), {"id": connection.id})
                await other.commit()

        pending = asyncio.create_task(rotate())
        try:
            await started.wait()
            await asyncio.sleep(0.1)
            assert not pending.done()
            await first.commit()
            await asyncio.wait_for(pending, 5)
            assert connection.status == "active"  # intentionally stale ORM object
            await set_tenant_context(first, str(i["company"]))
            from app.services.workspace.credential_contract import AuthSeederError

            with pytest.raises(AuthSeederError):
                await require_current_target(first, i["company"], identity)
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    print("REAL_RESTRICTED_ROLE shared row lock serialized rotation; stale ORM identity could not authorize")


@pytest.mark.asyncio
async def test_token_refresh_writer_not_blocked_by_external_command(installation, monkeypatch):  # noqa: F811
    import json

    from app.models.workspace import WorkspaceArtifact, WorkspaceRun
    from app.services.deploy_preview_service import compute_deploy_manifest

    i = installation
    monkeypatch.setattr(settings, "DEDICATED_RUNTIME", True)
    factory = async_sessionmaker(i["engine"], expire_on_commit=False)
    async with factory() as db:
        await set_tenant_context(db, str(i["company"]))
        user = await db.scalar(select(User).where(User.tenant_id == i["company"]))
        connection, credentials, identity = await make_target(db, i["company"])
        ws = Workspace(tenant_id=i["company"], name="Synthetic refresh contention", status="active", created_by=user.id)
        db.add(ws)
        await db.flush()
        cs = WorkspaceChangeSet(
            tenant_id=i["company"],
            workspace_id=ws.id,
            title="Refresh contention",
            status="approved",
            proposed_by=user.id,
        )
        db.add(cs)
        await db.flush()
        p = await compute_deploy_manifest(db=db, changeset_id=cs.id, tenant_id=i["company"], workspace_id=ws.id)
        for kind in ("suitecloud_validate", "jest_unit_test"):
            check = WorkspaceRun(
                tenant_id=i["company"],
                workspace_id=ws.id,
                changeset_id=cs.id,
                run_type=kind,
                status="passed",
                gate_status="pass",
                snapshot_hash=p["snapshot_sha"],
                triggered_by=user.id,
            )
            db.add(check)
            await db.flush()
            if kind == "suitecloud_validate":
                db.add(
                    WorkspaceArtifact(
                        tenant_id=i["company"],
                        run_id=check.id,
                        artifact_type="result_json",
                        content=json.dumps(identity),
                    )
                )
        run = await runner_service.create_run(db, i["company"], ws.id, "deploy_sandbox", user.id, changeset_id=cs.id)
        await db.commit()
        refreshed = False

        async def command(*_):
            nonlocal refreshed
            async with factory() as other:
                await set_tenant_context(other, str(i["company"]))
                await other.execute(text("SET LOCAL lock_timeout = '300ms'"))
                current = await other.get(Connection, connection.id)
                credentials["access_token"] = "synthetic-rotated-token"
                current.encrypted_credentials = encrypt_credentials(credentials)
                await other.commit()
                refreshed = True
            return 0, "", ""

        await set_tenant_context(db, str(i["company"]))
        with (
            patch.object(runner_service, "_run_subprocess", AsyncMock(side_effect=command)),
            patch(
                "app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run",
                AsyncMock(return_value=SeededCredentials(Path("/tmp/unused"), **identity)),
            ),
        ):
            await runner_service.execute_run(
                db,
                run.id,
                i["company"],
                extra_params={
                    "sandbox_id": "123-sb1",
                    "expected_snapshot_sha": p["snapshot_sha"],
                    "expected_manifest_sha": p["manifest_sha"],
                    "expected_target_identity": identity,
                },
            )
        assert refreshed and run.status == "passed"
    print("REAL_RESTRICTED_ROLE token refresh writer committed during synthetic external command")
