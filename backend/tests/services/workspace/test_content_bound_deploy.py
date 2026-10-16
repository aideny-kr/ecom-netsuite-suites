"""Receipts must describe the exact project executed, not just its identity."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from app.models.workspace import Workspace, WorkspaceChangeSet, WorkspaceFile
from app.services import deploy_service, runner_service
from app.services.deploy_preview_service import (
    DeployGateNotMetError,
    build_deploy_preview,
    mint_deploy_token,
    verify_and_consume_deploy_token,
)
from app.services.workspace.suitecloud_auth_seeder import SeededCredentials


@pytest_asyncio.fixture
async def checked_project(db, tenant_a, admin_user):
    user, _ = admin_user
    ws = Workspace(tenant_id=tenant_a.id, name="Content checks", status="active", created_by=user.id)
    db.add(ws)
    await db.flush()
    file = WorkspaceFile(
        tenant_id=tenant_a.id,
        workspace_id=ws.id,
        path="SuiteScripts/a.js",
        file_name="a.js",
        content="// checked\n",
        sha256_hash="a" * 64,
        size_bytes=11,
        is_directory=False,
    )
    cs = WorkspaceChangeSet(
        tenant_id=tenant_a.id, workspace_id=ws.id, title="Content checks", status="approved", proposed_by=user.id
    )
    db.add_all([file, cs])
    await db.flush()

    async def check(kind):
        run = await runner_service.create_run(
            db,
            tenant_id=tenant_a.id,
            workspace_id=ws.id,
            changeset_id=cs.id,
            triggered_by=user.id,
            run_type=kind,
        )
        with (
            patch.object(runner_service, "_run_subprocess", new=AsyncMock(return_value=(0, "", ""))),
            patch(
                "app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run",
                new=AsyncMock(return_value=SeededCredentials(path=Path("/tmp/unused"), account_id="123-sb1")),
            ),
        ):
            await runner_service.execute_run(db, run.id, tenant_a.id)
        run.created_at = datetime.now(timezone.utc)
        await db.flush()
        assert run.status == "passed"
        return run

    validation = await check("suitecloud_validate")
    tests = await check("jest_unit_test")
    return ws, cs, user, file, check, validation, tests


async def preview(db, project):
    ws, cs, user, *_ = project
    return await build_deploy_preview(
        db=db,
        changeset_id=cs.id,
        sandbox_id="123-sb1",
        require_assertions=False,
        tenant_id=ws.tenant_id,
        actor_id=user.id,
    )


@pytest.mark.asyncio
async def test_actual_check_receipts_match_preview_and_survive_reload(db, checked_project):
    ws, cs, user, file, check, validation, tests = checked_project
    result = await preview(db, checked_project)
    assert validation.snapshot_hash == tests.snapshot_hash == result["snapshot_sha"]
    await db.refresh(validation)
    await db.refresh(tests)
    assert validation.snapshot_hash == tests.snapshot_hash == result["snapshot_sha"]


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh", [None, "suitecloud_validate", "jest_unit_test"])
async def test_same_filecount_content_edit_blocks_preview(db, checked_project, refresh):
    ws, cs, user, file, check, *_ = checked_project
    # Keep count and cached sha unchanged: only the actual code changed.
    file.content = "// edited after checks\n"
    await db.flush()
    if refresh:
        await check(refresh)
    with pytest.raises(DeployGateNotMetError):
        await preview(db, checked_project)
    result = await deploy_service.check_deploy_prerequisites(db, cs.id, ws.tenant_id)
    assert result["allowed"] is False  # omission of expected hash cannot bypass freshness
    if refresh != "suitecloud_validate":
        assert result["gates"]["validate"]["status"] == "stale"
    if refresh != "jest_unit_test":
        assert result["gates"]["unit_tests"]["status"] == "stale"


@pytest.mark.asyncio
async def test_confirm_rejects_test_receipt_replaced_with_unbound_pass(db, checked_project):
    ws, cs, user, *_ = checked_project
    p = await preview(db, checked_project)
    minted = await mint_deploy_token(db=db, preview=p)
    run = await runner_service.create_run(
        db,
        tenant_id=ws.tenant_id,
        workspace_id=ws.id,
        changeset_id=cs.id,
        triggered_by=user.id,
        run_type="jest_unit_test",
    )
    run.created_at = datetime.now(timezone.utc)
    run.status = "passed"  # legacy or otherwise unbound evidence must fail closed
    await db.flush()
    with pytest.raises(DeployGateNotMetError):
        await verify_and_consume_deploy_token(
            db=db,
            jti=minted["jti"],
            confirmation_token=minted["confirmation_token"],
            actor_id=user.id,
            tenant_id=ws.tenant_id,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["materialized", "receipt", "no_pin"])
async def test_worker_rejects_drift_before_subprocess(db, checked_project, mutation):
    ws, cs, user, *_ = checked_project
    p = await preview(db, checked_project)
    run = await runner_service.create_run(
        db,
        tenant_id=ws.tenant_id,
        workspace_id=ws.id,
        changeset_id=cs.id,
        triggered_by=user.id,
        run_type="deploy_sandbox",
    )
    params = {
        "sandbox_id": "123-sb1",
        "expected_snapshot_sha": p["snapshot_sha"],
        "expected_manifest_sha": p["manifest_sha"],
        "require_assertions": False,
    }
    materialize = runner_service._materialize_snapshot

    async def mutate_materialized(db, run, directory):
        count = await materialize(db, run, directory)
        # Database/manifest still match: worker must check the files it will execute.
        (Path(directory) / "SuiteScripts/a.js").write_text("// unchecked\n")
        return count

    if mutation == "receipt":
        checked_project[-1].status = "failed"
        await db.flush()
    if mutation == "no_pin":
        params.pop("expected_snapshot_sha")
    command = AsyncMock(return_value=(0, "deploy succeeded", ""))
    with patch.object(runner_service, "_run_subprocess", new=command):
        if mutation == "materialized":
            with patch.object(runner_service, "_materialize_snapshot", side_effect=mutate_materialized):
                result = await runner_service.execute_run(db, run.id, ws.tenant_id, extra_params=params)
        else:
            result = await runner_service.execute_run(db, run.id, ws.tenant_id, extra_params=params)
    assert result.status == "error"
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_queued_worker_and_confirm_content_drift_is_blocked(db, checked_project):
    from app.services.deploy_preview_service import SnapshotDriftError

    ws, cs, user, file, *_ = checked_project
    p = await preview(db, checked_project)
    minted = await mint_deploy_token(db=db, preview=p)
    file.content = "// changed after preview\n"
    await db.flush()
    with pytest.raises((DeployGateNotMetError, SnapshotDriftError)):
        await verify_and_consume_deploy_token(
            db=db,
            jti=minted["jti"],
            confirmation_token=minted["confirmation_token"],
            actor_id=user.id,
            tenant_id=ws.tenant_id,
        )


@pytest.mark.asyncio
async def test_checks_from_another_account_cannot_authorize_target(db, checked_project):
    ws, cs, user, *_ = checked_project
    with pytest.raises(DeployGateNotMetError) as rejected:
        await build_deploy_preview(
            db=db,
            changeset_id=cs.id,
            sandbox_id="456-sb1",
            require_assertions=False,
            tenant_id=ws.tenant_id,
            actor_id=user.id,
        )
    assert rejected.value.gates["validate"]["status"] == "target_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("oauth_connection", [False, True])
async def test_deploy_cannot_use_ambient_cli_auth_even_with_passing_receipts(db, checked_project, oauth_connection):
    import json

    from app.core.encryption import encrypt_credentials
    from app.models.connection import Connection

    ws, cs, user, *_ = checked_project
    if oauth_connection:
        db.add(
            Connection(
                tenant_id=ws.tenant_id,
                provider="netsuite",
                label="Synthetic REST only",
                status="active",
                auth_type="oauth2",
                encrypted_credentials=encrypt_credentials({"account_id": "123_SB1", "access_token": "synthetic-only"}),
            )
        )
        await db.flush()
    p = await preview(db, checked_project)
    run = await runner_service.create_run(db, ws.tenant_id, ws.id, "deploy_sandbox", user.id, changeset_id=cs.id)
    command = AsyncMock(return_value=(0, "deployed", ""))
    with patch.object(runner_service, "_run_subprocess", new=command):
        result = await runner_service.execute_run(
            db,
            run.id,
            ws.tenant_id,
            extra_params={
                "sandbox_id": "123-sb1",
                "expected_snapshot_sha": p["snapshot_sha"],
                "expected_manifest_sha": p["manifest_sha"],
            },
        )
    assert result.status == "error"
    command.assert_not_awaited()
    artifacts = await runner_service.get_artifacts(db, run.id, ws.tenant_id)
    receipt = next(a for a in artifacts if a.artifact_type == "result_json")
    assert json.loads(receipt.content)["error_category"] == "AUTH_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", ["user", "company", "permission"])
async def test_queued_deploy_rechecks_authority(db, checked_project, revoke):
    from sqlalchemy import delete

    from app.models.tenant import Tenant
    from app.models.user import UserRole

    ws, cs, user, *_ = checked_project
    p = await preview(db, checked_project)
    run = await runner_service.create_run(
        db,
        tenant_id=ws.tenant_id,
        workspace_id=ws.id,
        changeset_id=cs.id,
        triggered_by=user.id,
        run_type="deploy_sandbox",
    )
    if revoke == "user":
        user.is_active = False
    elif revoke == "company":
        (await db.get(Tenant, ws.tenant_id)).is_active = False
    else:
        await db.execute(delete(UserRole).where(UserRole.user_id == user.id))
    await db.flush()
    command = AsyncMock(return_value=(0, "", ""))
    with patch.object(runner_service, "_run_subprocess", new=command):
        result = await runner_service.execute_run(
            db,
            run.id,
            ws.tenant_id,
            extra_params={
                "sandbox_id": "123-sb1",
                "expected_snapshot_sha": p["snapshot_sha"],
                "expected_manifest_sha": p["manifest_sha"],
            },
        )
    assert result.status == "error"
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_deploy_delivery_does_not_dispatch_again(db, checked_project):
    ws, cs, user, *_ = checked_project
    p = await preview(db, checked_project)
    run = await runner_service.create_run(
        db,
        tenant_id=ws.tenant_id,
        workspace_id=ws.id,
        changeset_id=cs.id,
        triggered_by=user.id,
        run_type="deploy_sandbox",
    )
    params = {
        "sandbox_id": "123-sb1",
        "expected_snapshot_sha": p["snapshot_sha"],
        "expected_manifest_sha": p["manifest_sha"],
    }
    command = AsyncMock(return_value=(0, "", ""))
    with (
        patch.object(runner_service, "_run_subprocess", new=command),
        patch(
            "app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run",
            new=AsyncMock(
                return_value=SeededCredentials(path=Path("/tmp/unused"), account_id="123-sb1", role_id="1234")
            ),
        ),
    ):
        await runner_service.execute_run(db, run.id, ws.tenant_id, extra_params=params)
        await runner_service.execute_run(db, run.id, ws.tenant_id, extra_params=params)
    assert command.await_count == 1
    assert run.status == "passed"
