# ruff: noqa: F811 -- pytest fixture imported intentionally
"""The projection version is durable signed authority, never guessed at delivery."""

import uuid

import pytest

from app.models.workspace import WorkspaceDeployToken
from app.services.deploy_preview_service import (
    DeployGateNotMetError,
    mint_deploy_token,
    verify_and_consume_deploy_token,
)
from tests.services.workspace.test_content_bound_deploy import checked_project, preview  # noqa: F401


@pytest.mark.asyncio
async def test_mint_refuses_a_preview_with_different_format(db, checked_project):
    p = await preview(db, checked_project)
    p["project_format"] = "future-v2"
    with pytest.raises(DeployGateNotMetError, match="preview again"):
        await mint_deploy_token(db=db, preview=p)


@pytest.mark.asyncio
async def test_format_is_stored_and_unsupported_values_are_rejected(db, checked_project):
    ws, cs, user, *_ = checked_project
    p = await preview(db, checked_project)
    assert p["project_format"] == "suite-studio-files-v1"
    minted = await mint_deploy_token(db=db, preview=p)
    row = await db.get(WorkspaceDeployToken, uuid.UUID(minted["jti"]))
    assert row.project_format == p["project_format"]
    row.project_format = "future-v2"
    await db.flush()
    with pytest.raises(DeployGateNotMetError):
        await verify_and_consume_deploy_token(
            db=db, jti=row.id, confirmation_token=minted["confirmation_token"], actor_id=user.id, tenant_id=ws.tenant_id
        )
    assert row.consumed_at is None


@pytest.mark.asyncio
async def test_format_is_cryptographically_bound_even_when_schema_gate_is_bypassed(db, checked_project):
    from app.services.chat.mutation_guard import verify_confirmation_token
    from app.services.deploy_preview_service import _canonical_payload

    ws, cs, user, *_ = checked_project
    minted = await mint_deploy_token(db=db, preview=await preview(db, checked_project))
    row = await db.get(WorkspaceDeployToken, uuid.UUID(minted["jti"]))
    payload = dict(
        jti=str(row.id),
        tenant_id=str(row.tenant_id),
        workspace_id=str(row.workspace_id),
        changeset_id=str(row.changeset_id),
        sandbox_id=row.sandbox_id,
        snapshot_sha=row.snapshot_sha,
        manifest_sha=row.manifest_sha,
        require_assertions=row.require_assertions,
        actor_id=str(row.actor_id),
        issued_at=row.issued_at.isoformat(),
        target_identity=row.target_identity,
        project_format=row.project_format,
    )
    for version, expected in [(row.project_format, True), (None, False), ("future-v2", False)]:
        payload["project_format"] = version
        assert (
            verify_confirmation_token(
                minted["confirmation_token"],
                session_id=str(user.id),
                payload_json=_canonical_payload(**payload),
                event_type="sandbox_deploy",
            )
            is expected
        )


@pytest.mark.asyncio
async def test_unbound_preview_requires_replacement_and_releases_slot(db, checked_project):
    ws, cs, user, *_ = checked_project
    p = await preview(db, checked_project)
    minted = await mint_deploy_token(db=db, preview=p)
    row = await db.get(WorkspaceDeployToken, uuid.UUID(minted["jti"]))
    row.project_format = None
    await db.flush()
    with pytest.raises(DeployGateNotMetError, match="preview again"):
        await verify_and_consume_deploy_token(
            db=db, jti=row.id, confirmation_token=minted["confirmation_token"], actor_id=user.id, tenant_id=ws.tenant_id
        )
    replacement = await mint_deploy_token(db=db, preview=await preview(db, checked_project))
    assert replacement["jti"] != minted["jti"] and row.consumed_reason == "superseded"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "future-v2"])
async def test_worker_denies_unbound_projection_before_credentials(db, checked_project, version):
    import json
    from unittest.mock import AsyncMock, patch

    from app.services import runner_service

    ws, cs, user, *_ = checked_project
    p = await preview(db, checked_project)
    run = await runner_service.create_run(db, ws.tenant_id, ws.id, "deploy_sandbox", user.id, changeset_id=cs.id)
    with patch("app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run", new=AsyncMock()) as seed:
        await runner_service.execute_run(
            db,
            run.id,
            ws.tenant_id,
            extra_params={
                "sandbox_id": "123-sb1",
                "expected_snapshot_sha": p["snapshot_sha"],
                "expected_manifest_sha": p["manifest_sha"],
                "expected_target_identity": p["target_identity"],
                "expected_project_format": version,
            },
        )
    seed.assert_not_awaited()
    assert run.status == "error"
    artifacts = await runner_service.get_artifacts(db, run.id, ws.tenant_id)
    result = json.loads(next(a.content for a in artifacts if a.artifact_type == "result_json"))
    assert result["error_category"] == "DEPLOY_GATE"


@pytest.mark.asyncio
async def test_dispatch_uses_persisted_format_and_rejects_legacy_queue(db, checked_project):
    from unittest.mock import patch

    from app.services import runner_service
    from app.services.workspace.deploy_dispatch import dispatch_confirmed_deploy

    ws, cs, user, *_ = checked_project
    minted = await mint_deploy_token(db=db, preview=await preview(db, checked_project))
    verified = await verify_and_consume_deploy_token(
        db=db,
        jti=uuid.UUID(minted["jti"]),
        confirmation_token=minted["confirmation_token"],
        actor_id=user.id,
        tenant_id=ws.tenant_id,
    )
    run = await runner_service.create_run(db, ws.tenant_id, ws.id, "deploy_sandbox", user.id, changeset_id=cs.id)
    row = verified["row"]
    row.consumed_run_id = run.id
    await db.commit()
    with patch("app.workers.tasks.workspace_run.workspace_run_task.delay") as send:
        await dispatch_confirmed_deploy(db, run_id=run.id, tenant_id=ws.tenant_id, actor_id=user.id)
        assert send.call_args.kwargs["extra_params"]["expected_project_format"] == row.project_format
    row.project_format = None
    await db.commit()
    with patch("app.workers.tasks.workspace_run.workspace_run_task.delay") as send:
        with pytest.raises(ValueError, match="preview again"):
            await dispatch_confirmed_deploy(db, run_id=run.id, tenant_id=ws.tenant_id, actor_id=user.id)
        send.assert_not_called()
