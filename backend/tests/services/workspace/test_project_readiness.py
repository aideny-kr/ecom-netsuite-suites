# ruff: noqa: F811 -- fixture imported intentionally
"""Reject unsupported projects before minting deployment authority."""

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select

from app.models.workspace import WorkspaceDeployToken, WorkspaceFile, WorkspacePatch
from app.services import runner_service
from app.services.deploy_preview_service import (
    DeployGateNotMetError,
    compute_deploy_manifest,
    mint_deploy_token,
    verify_and_consume_deploy_token,
)
from tests.services.workspace.test_content_bound_deploy import checked_project, preview  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,reason",
    [
        ("CustomScripts/a.js", "unsupported_projection"),
        ("FileCabinet/SuiteScripts/my script.js", "unsupported_projection"),
        ("SuiteScripts/a.test.js", "test_path_in_script_tree"),
        ("SuiteScripts/a.spec.js", "test_path_in_script_tree"),
        ("SuiteScripts/__tests__/a.js", "test_path_in_script_tree"),
        ("SuiteScripts/__mocks__/a.js", "test_path_in_script_tree"),
        ("SuiteScripts/tests/a.js", "test_path_in_script_tree"),
        ("SuiteScripts/test/a.js", "test_path_in_script_tree"),
        ("FileCabinet/SuiteScripts/sub/EXAMPLE.TEST.js", "test_path_in_script_tree"),
        ("FileCabinet/SuiteScripts/sub/__snapshots__/a.js", "test_path_in_script_tree"),
        ("Objects/customscript_a.xml", "unsupported_projection"),
    ],
)
async def test_preview_rejects_unsupported_snapshot_before_token(db, checked_project, path, reason):
    ws, cs, user, _, check, *_ = checked_project
    db.add(
        WorkspaceFile(
            tenant_id=ws.tenant_id,
            workspace_id=ws.id,
            path=path,
            file_name=path.rsplit("/", 1)[-1],
            content="synthetic",
            is_directory=False,
        )
    )
    await db.flush()
    await check("suitecloud_validate")
    await check("jest_unit_test")
    with pytest.raises(DeployGateNotMetError) as exc:
        await mint_deploy_token(db=db, preview=await preview(db, checked_project))
    assert exc.value.gates["project"]["status"] == "blocked"
    assert exc.value.gates["project"]["reason"] == reason
    assert (
        not (await db.execute(select(WorkspaceDeployToken).where(WorkspaceDeployToken.changeset_id == cs.id)))
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_noop_preview_is_blocked_even_with_passing_checks(db, checked_project):
    ws, cs, _, _, check, *_ = checked_project
    await db.execute(delete(WorkspacePatch).where(WorkspacePatch.changeset_id == cs.id))
    await db.flush()
    await check("suitecloud_validate")
    await check("jest_unit_test")
    with pytest.raises(DeployGateNotMetError, match="file-only"):
        await preview(db, checked_project)


@pytest.mark.asyncio
async def test_preview_binds_auxiliary_bytes_and_reports_projection_without_source(db, checked_project):
    ws, cs, _, _, check, *_ = checked_project
    db.add(
        WorkspaceFile(
            tenant_id=ws.tenant_id,
            workspace_id=ws.id,
            path="tests/local.test.js",
            file_name="local.test.js",
            content="local-only synthetic test",
            is_directory=False,
        )
    )
    await db.flush()
    await check("suitecloud_validate")
    await check("jest_unit_test")
    p = await preview(db, checked_project)
    assert p["gates"]["project"]["status"] == "pass"
    assert p["gates"]["project"]["format"] == "suite-studio-files-v1"
    assert p["gates"]["project"]["snapshot_sha"] != p["snapshot_sha"]
    assert len(p["gates"]["project"]["project_sha"]) == 64
    assert "local-only synthetic test" not in json.dumps(p)
    assert "files" not in p


@pytest.mark.asyncio
async def test_pre_readiness_signed_noop_token_cannot_be_consumed(db, checked_project):
    ws, cs, user, _, check, *_ = checked_project
    # Recreate the old signed payload for a no-op snapshot, with fresh checks
    # and the current format/identity, so this proves more than drift rejection.
    p = await preview(db, checked_project)
    await db.execute(delete(WorkspacePatch).where(WorkspacePatch.changeset_id == cs.id))
    await db.flush()
    await check("suitecloud_validate")
    await check("jest_unit_test")
    p.update(await compute_deploy_manifest(db=db, changeset_id=cs.id, tenant_id=ws.tenant_id, workspace_id=ws.id))
    minted = await mint_deploy_token(db=db, preview=p)
    with pytest.raises(DeployGateNotMetError, match="file-only"):
        await verify_and_consume_deploy_token(
            db=db,
            jti=uuid.UUID(minted["jti"]),
            confirmation_token=minted["confirmation_token"],
            actor_id=user.id,
            tenant_id=ws.tenant_id,
        )
    row = await db.get(WorkspaceDeployToken, uuid.UUID(minted["jti"]))
    assert row.consumed_at is None and row.consumed_run_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["noop", "test_path", "synced_path"])
@pytest.mark.parametrize("entrypoint", ["dispatch", "worker"])
async def test_old_current_format_queue_is_rechecked_before_delivery_or_credentials(
    db, checked_project, mutation, entrypoint
):
    ws, cs, user, _, check, *_ = checked_project
    p = await preview(db, checked_project)
    if mutation == "noop":
        await db.execute(delete(WorkspacePatch).where(WorkspacePatch.changeset_id == cs.id))
    else:
        path = "SuiteScripts/a.test.js" if mutation == "test_path" else "CustomScripts/a.js"
        db.add(
            WorkspaceFile(
                tenant_id=ws.tenant_id,
                workspace_id=ws.id,
                path=path,
                file_name="a.js",
                content="synthetic",
                is_directory=False,
            )
        )
    await db.flush()
    await check("suitecloud_validate")
    await check("jest_unit_test")
    p.update(await compute_deploy_manifest(db=db, changeset_id=cs.id, tenant_id=ws.tenant_id, workspace_id=ws.id))
    minted = await mint_deploy_token(db=db, preview=p)
    run = await runner_service.create_run(db, ws.tenant_id, ws.id, "deploy_sandbox", user.id, changeset_id=cs.id)
    token = await db.get(WorkspaceDeployToken, uuid.UUID(minted["jti"]))
    token.consumed_at = datetime.now(timezone.utc)
    token.consumed_reason = "confirmed"
    token.consumed_run_id = run.id
    await db.commit()
    if entrypoint == "dispatch":
        from app.services.workspace.deploy_dispatch import dispatch_confirmed_deploy

        with patch("app.workers.tasks.workspace_run.workspace_run_task.delay") as send:
            with pytest.raises(ValueError, match="project"):
                await dispatch_confirmed_deploy(db, run_id=run.id, tenant_id=ws.tenant_id, actor_id=user.id)
            send.assert_not_called()
        assert run.status == "queued"
    else:
        with patch("app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run", AsyncMock()) as seed:
            await runner_service.execute_run(
                db,
                run.id,
                ws.tenant_id,
                extra_params={
                    "sandbox_id": p["sandbox_id"],
                    "expected_snapshot_sha": p["snapshot_sha"],
                    "expected_manifest_sha": p["manifest_sha"],
                    "expected_project_format": p["project_format"],
                    "expected_target_identity": p["target_identity"],
                },
            )
        seed.assert_not_awaited()
        assert run.status == "error"


@pytest.mark.asyncio
async def test_uat_report_does_not_call_noop_project_ready(client, db, checked_project, admin_user):
    ws, cs, _, _, check, *_ = checked_project
    await db.execute(delete(WorkspacePatch).where(WorkspacePatch.changeset_id == cs.id))
    await db.flush()
    await check("suitecloud_validate")
    await check("jest_unit_test")
    _, headers = admin_user
    response = await client.get(f"/api/v1/changesets/{cs.id}/uat-report", headers=headers)
    assert response.status_code == 200
    assert response.json()["overall_status"] != "ready_for_deploy"
    assert response.json()["gates"]["project"] == "blocked"
