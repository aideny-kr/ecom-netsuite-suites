"""Durable target routing; synthetic receipts grant no deployment authority."""

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select

from app.models.workspace import WorkspaceArtifact
from app.services import runner_service
from app.services.workspace import sdk_validation_dispatch as dispatch
from tests.services.workspace.test_content_bound_deploy import checked_project  # noqa: F401
from tests.services.workspace.test_sdk_validation_attempt import journal, synthetic_receipt  # noqa: F401


async def bind(db, journal):  # noqa: F811
    attempts, args, run, *_ = journal
    await attempts.bind_validation_connection(
        db, tenant_id=args["tenant_id"], run_id=run.id, connection_id=args["connection_id"]
    )
    return attempts, args, run


@pytest.mark.asyncio
async def test_selected_worker_ignores_queue_identity_and_routes_once(db, journal, monkeypatch):  # noqa: F811
    from app.workers.tasks import workspace_run

    attempts, args, run = await bind(db, journal)
    monkeypatch.setattr(dispatch, "validation_runtime", lambda: args["runtime"])

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(workspace_run, "worker_async_session", session)
    poisoned = dict(connection_id="foreign", runtime="injected", receipt_key="private", image="host")
    with (
        patch.object(
            attempts.credential_executor, "validate_project", AsyncMock(side_effect=lambda **kw: synthetic_receipt(kw))
        ) as broker,
        patch.object(runner_service, "execute_run", AsyncMock(side_effect=AssertionError("legacy executor reached"))),
    ):
        for _ in range(2):
            result = await workspace_run._execute(str(args["tenant_id"]), str(run.id), extra_params=poisoned)
            assert result["status"] == "passed"
        assert broker.await_count == 1
        assert broker.call_args.kwargs["context"]["connection_id"] == str(args["connection_id"])
    assert run.gate_status == "block"
    evidence = await attempts.read_validation_receipt(
        db, tenant_id=args["tenant_id"], run_id=run.id, runtime=args["runtime"]
    )
    assert evidence["server_validation"] is True
    assert not (
        await db.scalars(
            select(WorkspaceArtifact).where(
                WorkspaceArtifact.run_id == run.id, WorkspaceArtifact.artifact_type == "result_json"
            )
        )
    ).all()


@pytest.mark.asyncio
async def test_default_runtime_blocks_selected_run_without_fallback(db, journal):  # noqa: F811
    attempts, args, run = await bind(db, journal)
    with (
        patch.object(attempts.credential_executor, "validate_project", AsyncMock()) as broker,
        patch.object(runner_service, "execute_run", AsyncMock(side_effect=AssertionError("fallback"))),
    ):
        for _ in range(2):
            result = await dispatch.execute_workspace_run(db, tenant_id=args["tenant_id"], run_id=run.id)
            assert result.status == "failed" and result.gate_status == "block"
        broker.assert_not_awaited()
    artifacts = (await db.scalars(select(WorkspaceArtifact).where(WorkspaceArtifact.run_id == run.id))).all()
    assert [a.content for a in artifacts if a.artifact_type == "stderr"] == [
        "SDK validation runtime is not enabled; reviewed custody, egress and sandbox acceptance are required"
    ]
    assert not any(a.artifact_type == attempts.ATTEMPT for a in artifacts)


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["connection", "key", "duplicate", "hash", "context", "target", "missing"])
async def test_stale_or_corrupt_selection_never_dispatches(db, journal, monkeypatch, drift):  # noqa: F811
    from app.core.encryption import encrypt_credentials

    attempts, args, run = await bind(db, journal)
    _, _, _, connection, credentials, _ = journal
    row = await db.scalar(
        select(WorkspaceArtifact).where(
            WorkspaceArtifact.run_id == run.id, WorkspaceArtifact.artifact_type == attempts.SELECTION
        )
    )
    if drift == "connection":
        connection.status = "revoked"
    elif drift == "key":
        credentials["suitecloud_ci"]["certificate_id"] = "rotated"
        connection.encrypted_credentials = encrypt_credentials(credentials)
    elif drift == "duplicate":
        db.add(
            WorkspaceArtifact(
                tenant_id=run.tenant_id,
                run_id=run.id,
                artifact_type=attempts.SELECTION,
                content=row.content,
                size_bytes=row.size_bytes,
                sha256_hash=row.sha256_hash,
            )
        )
    elif drift == "hash":
        row.sha256_hash = "0" * 64
    elif drift == "missing":
        await db.delete(row)
    else:
        value = json.loads(row.content)
        value[drift]["actor_id" if drift == "context" else "role_id"] = "0"
        row.content = attempts._content(value)
        row.size_bytes = len(row.content)
        row.sha256_hash = hashlib.sha256(row.content.encode()).hexdigest()
    await db.flush()
    monkeypatch.setattr(dispatch, "validation_runtime", lambda: args["runtime"])
    with (
        patch.object(attempts.credential_executor, "validate_project", AsyncMock()) as broker,
        patch.object(runner_service, "execute_run", AsyncMock(side_effect=AssertionError("legacy fallback"))),
    ):
        result = await dispatch.execute_workspace_run(db, tenant_id=args["tenant_id"], run_id=run.id)
        assert result.status == "failed" and result.gate_status == "block"
        broker.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["delete", "replace"])
async def test_selection_drift_after_broker_invalidates_receipt_without_replay(db, journal, monkeypatch, mutation):  # noqa: F811
    attempts, args, run = await bind(db, journal)
    monkeypatch.setattr(dispatch, "validation_runtime", lambda: args["runtime"])

    async def broker(**kwargs):
        envelope = synthetic_receipt(kwargs)
        where = (WorkspaceArtifact.run_id == run.id, WorkspaceArtifact.artifact_type == attempts.SELECTION)
        if mutation == "delete":
            await db.execute(delete(WorkspaceArtifact).where(*where))
        else:
            row = await db.scalar(select(WorkspaceArtifact).where(*where))
            value = json.loads(row.content)
            value["target"]["role_id"] = "12345"
            row.content = attempts._content(value)
            row.size_bytes = len(row.content)
            row.sha256_hash = hashlib.sha256(row.content.encode()).hexdigest()
        await db.flush()
        return envelope

    with patch.object(attempts.credential_executor, "validate_project", AsyncMock(side_effect=broker)) as call:
        for _ in range(2):
            result = await dispatch.execute_workspace_run(db, tenant_id=args["tenant_id"], run_id=run.id)
            assert result.status == "failed" and result.gate_status == "block"
        assert call.await_count == 1


@pytest.mark.asyncio
async def test_selected_cancellation_retains_claim_and_never_falls_back(db, journal, monkeypatch):  # noqa: F811
    attempts, args, run = await bind(db, journal)
    monkeypatch.setattr(dispatch, "validation_runtime", lambda: args["runtime"])
    with (
        patch.object(
            attempts.credential_executor, "validate_project", AsyncMock(side_effect=asyncio.CancelledError())
        ) as call,
        patch.object(runner_service, "execute_run", AsyncMock(side_effect=AssertionError("fallback"))),
    ):
        with pytest.raises(asyncio.CancelledError):
            await dispatch.execute_workspace_run(db, tenant_id=args["tenant_id"], run_id=run.id)
        assert run.status == "running"
        await dispatch.execute_workspace_run(db, tenant_id=args["tenant_id"], run_id=run.id)
        assert call.await_count == 1


@pytest.mark.asyncio
async def test_http_binds_before_dispatch_and_rejects_foreign_connection(db, journal, client, admin_user, tenant_b):  # noqa: F811
    from tests.workspace_identity import make_target

    attempts, args, _, _, _, project = journal
    ws, cs, *_ = project
    _, headers = admin_user
    foreign, _, _ = await make_target(db, tenant_b.id)
    with patch("app.workers.tasks.workspace_run.workspace_run_task.delay") as queued:
        rejected = await client.post(
            f"/api/v1/changesets/{cs.id}/validate", headers=headers, json={"connection_id": str(foreign.id)}
        )
        assert rejected.status_code == 400
        queued.assert_not_called()
        good = await client.post(
            f"/api/v1/changesets/{cs.id}/validate", headers=headers, json={"connection_id": str(args["connection_id"])}
        )
        assert good.status_code == 202, good.text
        run_id = good.json()["id"]
        assert queued.call_args.kwargs["run_id"] == run_id
        assert "extra_params" not in queued.call_args.kwargs
        row = await db.scalar(
            select(WorkspaceArtifact).where(
                WorkspaceArtifact.run_id == run_id, WorkspaceArtifact.artifact_type == attempts.SELECTION
            )
        )
        assert json.loads(row.content)["context"]["connection_id"] == str(args["connection_id"])
        bad = await client.post(
            f"/api/v1/changesets/{cs.id}/validate",
            headers=headers,
            json={"connection_id": str(args["connection_id"]), "runtime": {"receipt_key": "injected"}},
        )
        assert bad.status_code == 422
        assert queued.call_count == 1


@pytest.mark.asyncio
async def test_unselected_worker_preserves_legacy_closed_path(db, journal):  # noqa: F811
    attempts, args, run, *_ = journal
    with patch.object(attempts.credential_executor, "validate_project", AsyncMock()) as broker:
        result = await dispatch.execute_workspace_run(
            db, tenant_id=args["tenant_id"], run_id=run.id, extra_params={"connection_id": str(args["connection_id"])}
        )
        assert result.status == "failed" and result.gate_status == "block"
        broker.assert_not_awaited()


@pytest.mark.asyncio
async def test_binding_cannot_be_replaced_or_reused_for_other_run(db, journal):  # noqa: F811
    attempts, args, run = await bind(db, journal)
    with pytest.raises(attempts.ValidationAttemptError, match="already exists"):
        await bind(db, journal)
    run.run_type = "jest_unit_test"
    await db.flush()
    with pytest.raises(attempts.ValidationAttemptError):
        await attempts.bind_validation_connection(
            db, tenant_id=args["tenant_id"], run_id=run.id, connection_id=args["connection_id"]
        )


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("CREDENTIAL_EXECUTOR_FAKE_IMAGE"), reason="owned synthetic Docker image absent")
async def test_selected_dispatch_through_actual_disposable_broker(db, journal, monkeypatch):  # noqa: F811
    attempts, args, run = await bind(db, journal)
    runtime = attempts.ValidationRuntime(
        os.environ["CREDENTIAL_EXECUTOR_FAKE_IMAGE"],
        os.environ["CREDENTIAL_EXECUTOR_TEST_SOCKET"],
        os.environ["CREDENTIAL_EXECUTOR_TEST_DOCKER"],
        b"r" * 32,
    )
    monkeypatch.setattr(dispatch, "validation_runtime", lambda: runtime)
    result = await dispatch.execute_workspace_run(db, tenant_id=args["tenant_id"], run_id=run.id)
    assert result.status == "passed" and result.gate_status == "block"
    assert (await attempts.read_validation_receipt(db, tenant_id=args["tenant_id"], run_id=run.id, runtime=runtime))[
        "server_validation"
    ] is True
