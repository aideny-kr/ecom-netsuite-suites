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
from app.models.workspace import Workspace, WorkspaceChangeSet, WorkspaceDeployToken, WorkspaceFile, WorkspacePatch
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
        await db.flush()
        db.add(
            WorkspacePatch(
                tenant_id=i["company"],
                changeset_id=cs.id,
                file_path="SuiteScripts/new.js",
                operation="create",
                new_content="// inert reviewed patch\n",
                baseline_sha256="",
                apply_order=0,
            )
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
        assert p["gates"]["project"]["status"] == "pass"
        assert len(p["gates"]["project"]["project_sha"]) == 64
        minted = await mint_deploy_token(db=db, preview=p)
        await db.commit()
    async with factory() as db:
        await set_tenant_context(db, str(i["company"]))
        token = await db.get(WorkspaceDeployToken, uuid.UUID(minted["jti"]))
        assert token.target_identity == identity
        assert token.project_format == "suite-studio-files-v1"
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
                current = await other.get(Connection, connection.id)
                current.status = "revoked"
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
@pytest.mark.parametrize("kind", ["deploy_sandbox", "suitecloud_validate"])
async def test_token_refresh_writer_not_blocked_by_external_command(installation, monkeypatch, kind):  # noqa: F811
    requested_kind = kind
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
        db.add(
            WorkspacePatch(
                tenant_id=i["company"],
                changeset_id=cs.id,
                file_path="SuiteScripts/refresh.js",
                operation="create",
                new_content="// synthetic refresh proof\n",
                baseline_sha256="",
                apply_order=0,
            )
        )
        await db.flush()
        p = await compute_deploy_manifest(db=db, changeset_id=cs.id, tenant_id=i["company"], workspace_id=ws.id)
        for check_kind in ("suitecloud_validate", "jest_unit_test"):
            check = WorkspaceRun(
                tenant_id=i["company"],
                workspace_id=ws.id,
                changeset_id=cs.id,
                run_type=check_kind,
                status="passed",
                gate_status="pass",
                snapshot_hash=p["snapshot_sha"],
                triggered_by=user.id,
            )
            db.add(check)
            await db.flush()
            if check_kind == "suitecloud_validate":
                db.add(
                    WorkspaceArtifact(
                        tenant_id=i["company"],
                        run_id=check.id,
                        artifact_type="result_json",
                        content=json.dumps(identity),
                    )
                )
        run = await runner_service.create_run(db, i["company"], ws.id, kind, user.id, changeset_id=cs.id)
        assert run.run_type == requested_kind
        await db.commit()
        refreshed = False
        attempts = 0
        validation_redeliveries = 0

        async def command(*_):
            nonlocal refreshed, attempts, validation_redeliveries
            attempts += 1
            assert attempts == 1
            async with factory() as other:
                await set_tenant_context(other, str(i["company"]))
                await other.execute(text("SET LOCAL lock_timeout = '300ms'"))
                if kind == "suitecloud_validate":
                    duplicate = await runner_service.execute_run(other, run.id, i["company"])
                    assert duplicate.status == "running"
                    validation_redeliveries += 1
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
                    "expected_project_format": "suite-studio-files-v1",
                    "expected_target_identity": identity,
                },
            )
        assert refreshed and run.status == "passed" and attempts == 1
        assert validation_redeliveries == (1 if requested_kind == "suitecloud_validate" else 0)
        print("VERIFIED_KIND", requested_kind, "VALIDATION_REDELIVERIES", validation_redeliveries)
    print("REAL_RESTRICTED_ROLE token refresh writer committed during synthetic external command")


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["http", "mcp"])
@pytest.mark.parametrize("suppress_confirm_commit", [False, True])
async def test_separate_session_observes_confirmation_before_blocked_delivery(
    installation,  # noqa: F811 -- fixture
    monkeypatch,
    channel,
    suppress_confirm_commit,
):
    """Real commit proof plus negative control; no same-session savepoint proof."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import func

    from app.core.database import get_db
    from app.core.security import create_access_token
    from app.main import create_app
    from app.mcp.tools.workspace_tools import execute_deploy_sandbox_confirm
    from app.models.workspace import WorkspaceRun
    from app.services.workspace.deploy_dispatch import dispatch_confirmed_deploy

    i = installation
    monkeypatch.setattr(settings, "DEDICATED_RUNTIME", True)
    factory = async_sessionmaker(i["engine"], expire_on_commit=False)
    async with factory() as db:
        await set_tenant_context(db, str(i["company"]))
        assert tuple(
            (await db.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()
        ) == (False, False)
        user = await db.scalar(select(User).where(User.tenant_id == i["company"]))
        _, _, identity = await make_target(db, i["company"])
        ws = Workspace(
            tenant_id=i["company"], name="Synthetic separate-session delivery", status="active", created_by=user.id
        )
        db.add(ws)
        await db.flush()
        cs = WorkspaceChangeSet(
            tenant_id=i["company"],
            workspace_id=ws.id,
            title="Durable confirmation",
            status="approved",
            proposed_by=user.id,
        )
        db.add(cs)
        await db.flush()
        db.add(
            WorkspacePatch(
                tenant_id=i["company"],
                changeset_id=cs.id,
                file_path="SuiteScripts/durable.js",
                operation="create",
                new_content="// synthetic exact input\n",
                baseline_sha256="",
                apply_order=0,
            )
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
        preview = await build_deploy_preview(
            db=db,
            changeset_id=cs.id,
            sandbox_id="123-sb1",
            require_assertions=False,
            tenant_id=i["company"],
            actor_id=user.id,
        )
        minted = await mint_deploy_token(db=db, preview=preview)
        await db.commit()
        workspace_id, changeset_id, actor_id = ws.id, cs.id, user.id
    observed = []

    async def observer_then_edit(request_db, **kwargs):
        async with factory() as other:
            await set_tenant_context(other, str(i["company"]))
            token = await other.get(WorkspaceDeployToken, uuid.UUID(minted["jti"]))
            assert token.consumed_at is not None and token.consumed_run_id == kwargs["run_id"], (
                "confirmation commit not visible"
            )
            durable = await other.get(WorkspaceRun, kwargs["run_id"])
            assert durable is not None and durable.status == "queued", "confirmation commit not visible"
            observed.append(durable.id)
            other.add(
                WorkspaceFile(
                    tenant_id=i["company"],
                    workspace_id=workspace_id,
                    path="SuiteScripts/later.js",
                    file_name="later.js",
                    content="// independent concurrent edit",
                    is_directory=False,
                )
            )
            await other.commit()
        return await dispatch_confirmed_deploy(request_db, **kwargs)

    params = {"jti": minted["jti"], "confirmation_token": minted["confirmation_token"]}

    async def invoke():
        if channel == "mcp":
            async with factory() as request_db:
                await set_tenant_context(request_db, str(i["company"]))
                if suppress_confirm_commit:
                    monkeypatch.setattr(request_db, "commit", request_db.flush)
                return await execute_deploy_sandbox_confirm(
                    params, {"db": request_db, "tenant_id": str(i["company"]), "actor_id": str(actor_id)}
                )
        app = create_app()

        async def request_db_dependency():
            async with factory() as request_db:
                if suppress_confirm_commit:
                    monkeypatch.setattr(request_db, "commit", request_db.flush)
                yield request_db

        app.dependency_overrides[get_db] = request_db_dependency
        headers = {
            "Authorization": "Bearer " + create_access_token({"sub": str(actor_id), "tenant_id": str(i["company"])})
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://company.example") as client:
            response = await client.post(
                f"/api/v1/changesets/{changeset_id}/deploy-sandbox/confirm", json=params, headers=headers
            )
            assert response.status_code == 409, response.text
            return response.json()["detail"]

    with (
        patch("app.services.workspace.deploy_dispatch.dispatch_confirmed_deploy", observer_then_edit),
        patch("app.workers.tasks.workspace_run.workspace_run_task.delay") as send,
    ):
        if suppress_confirm_commit:
            with pytest.raises(AssertionError, match="confirmation commit not visible"):
                await invoke()
            assert not observed
        else:
            result = await invoke()
            assert result["code"] == "deployment_delivery_blocked" and result["status"] == "queued"
            assert len(observed) == 1 and str(observed[0]) == result["run_id"]
        send.assert_not_called()
    async with factory() as other:
        await set_tenant_context(other, str(i["company"]))
        token = await other.get(WorkspaceDeployToken, uuid.UUID(minted["jti"]))
        count = await other.scalar(
            select(func.count())
            .select_from(WorkspaceRun)
            .where(WorkspaceRun.changeset_id == changeset_id, WorkspaceRun.run_type == "deploy_sandbox")
        )
        assert count == (0 if suppress_confirm_commit else 1)
        if suppress_confirm_commit:
            assert token.consumed_at is None and token.consumed_run_id is None
        else:
            assert token.consumed_at is not None and token.consumed_run_id == observed[0]
            assert (await other.get(WorkspaceRun, observed[0])).status == "queued"
    print(f"REAL_RESTRICTED_ROLE separate sessions channel={channel} negative_control={suppress_confirm_commit}")
