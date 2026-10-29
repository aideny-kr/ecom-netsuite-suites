"""Selected worker routing through separate committed restricted-role sessions."""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.core.database import set_tenant_context
from app.models.connection import Connection
from app.models.workspace import WorkspaceArtifact, WorkspaceRun
from app.services import runner_service
from app.services.workspace import sdk_validation_attempt as attempts
from app.services.workspace import sdk_validation_dispatch as dispatch
from app.workers.tasks import workspace_run
from tests.integration.test_sdk_validation_attempt_runtime import seed
from tests.services.workspace.test_sdk_validation_attempt import synthetic_receipt
from tests.test_dedicated_runtime import installation  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["verified", "revoked", "cancel", "selection_deleted", "changeset_unlinked"])
async def test_committed_selection_worker_exclusivity_and_recovery(installation, monkeypatch, mode):  # noqa: F811
    monkeypatch.setattr(settings, "DEDICATED_RUNTIME", True)
    factory = async_sessionmaker(installation["engine"], expire_on_commit=False)
    args, _, _ = await seed(factory, installation["company"])
    async with factory() as db:
        await set_tenant_context(db, str(args["tenant_id"]))
        await attempts.bind_validation_connection(db, **args)
        await db.commit()
    runtime = attempts.ValidationRuntime("sha256:" + "a" * 64, "/tmp/synthetic.sock", "/usr/bin/docker", b"r" * 32)
    monkeypatch.setattr(dispatch, "validation_runtime", lambda: runtime)
    monkeypatch.setattr(workspace_run, "worker_async_session", factory)

    async def broker(**kwargs):
        result = synthetic_receipt(kwargs)
        async with factory() as observer:
            await set_tenant_context(observer, str(args["tenant_id"]))
            run = await observer.get(WorkspaceRun, args["run_id"])
            selection = await observer.scalar(
                select(WorkspaceArtifact).where(
                    WorkspaceArtifact.run_id == run.id, WorkspaceArtifact.artifact_type == attempts.SELECTION
                )
            )
            claim = await observer.scalar(
                select(WorkspaceArtifact).where(
                    WorkspaceArtifact.run_id == run.id, WorkspaceArtifact.artifact_type == attempts.ATTEMPT
                )
            )
            assert run.status == "running" and selection and claim
            assert json.loads(claim.content)["selection_sha"] == selection.sha256_hash
            # Independent worker delivery sees the committed claim and returns
            # immediately; neither SDK nor legacy executor may be dispatched.
            duplicate = await asyncio.wait_for(workspace_run._execute(str(run.tenant_id), str(run.id)), timeout=2)
            assert duplicate["status"] == "running"
            if mode == "revoked":
                connection = await observer.get(Connection, args["connection_id"])
                connection.status = "revoked"
            elif mode == "selection_deleted":
                await observer.delete(selection)
            elif mode == "changeset_unlinked":
                run.changeset_id = None
            await observer.commit()
        if mode == "cancel":
            raise asyncio.CancelledError()
        return result

    with (
        patch.object(attempts.credential_executor, "validate_project", AsyncMock(side_effect=broker)) as call,
        patch.object(runner_service, "execute_run", AsyncMock(side_effect=AssertionError("legacy executor"))),
    ):
        if mode == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await workspace_run._execute(str(args["tenant_id"]), str(args["run_id"]))
            expected = "running"
        else:
            first = await workspace_run._execute(str(args["tenant_id"]), str(args["run_id"]))
            expected = "passed" if mode == "verified" else ("running" if mode == "changeset_unlinked" else "failed")
            assert first["status"] == expected
        result = await workspace_run._execute(str(args["tenant_id"]), str(args["run_id"]))
        assert result["status"] == expected and call.await_count == 1
    async with factory() as observer:
        await set_tenant_context(observer, str(args["tenant_id"]))
        run = await observer.get(WorkspaceRun, args["run_id"])
        assert run.status == expected and run.gate_status == "block"
        if mode == "verified":
            receipt = await attempts.read_validation_receipt(
                observer, tenant_id=run.tenant_id, run_id=run.id, runtime=runtime
            )
            assert receipt["server_validation"] is True
        await observer.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [False, True])
async def test_http_selection_commit_or_refusal_rollback_is_visible_to_separate_session(
    installation,  # noqa: F811
    monkeypatch,
    valid,
):
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import func

    from app.core.database import get_db
    from app.core.security import create_access_token
    from app.main import create_app

    monkeypatch.setattr(settings, "DEDICATED_RUNTIME", True)
    factory = async_sessionmaker(installation["engine"], expire_on_commit=False)
    args, _, _ = await seed(factory, installation["company"])
    async with factory() as observer:
        await set_tenant_context(observer, str(args["tenant_id"]))
        run = await observer.get(WorkspaceRun, args["run_id"])
        changeset_id = run.changeset_id
        token = create_access_token({"sub": str(run.triggered_by), "tenant_id": str(run.tenant_id)})

    async def request_db():
        async with factory() as db:
            await set_tenant_context(db, str(args["tenant_id"]))
            yield db  # session close rolls a refused request back, as in get_db

    app = create_app()
    app.dependency_overrides[get_db] = request_db
    observed = []

    async def assert_committed(**kwargs):
        async with factory() as observer:
            await set_tenant_context(observer, str(args["tenant_id"]))
            selected_run = await observer.get(WorkspaceRun, uuid.UUID(kwargs["run_id"]))
            selection = await observer.scalar(
                select(WorkspaceArtifact).where(
                    WorkspaceArtifact.run_id == selected_run.id, WorkspaceArtifact.artifact_type == attempts.SELECTION
                )
            )
            assert selected_run.validator_engine == "suitecloud_sdk_pinned" and selection
            observed.append(selected_run.id)

    # .delay is synchronous; an async assertion is run after request completion
    # from its captured args, against a genuinely independent connection.
    with patch.object(workspace_run.workspace_run_task, "delay") as queued:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/changesets/{changeset_id}/validate",
                headers={"Authorization": f"Bearer {token}"},
                json={"connection_id": str(args["connection_id"] if valid else uuid.uuid4())},
            )
        assert response.status_code == (202 if valid else 400), response.text
        if valid:
            assert queued.call_count == 1
            await assert_committed(**queued.call_args.kwargs)
        else:
            queued.assert_not_called()
    async with factory() as observer:
        await set_tenant_context(observer, str(args["tenant_id"]))
        count = await observer.scalar(select(func.count()).select_from(WorkspaceRun))
        assert count == (2 if valid else 1), "A refused request must leave no orphan run"
        assert len(observed) == int(valid)
