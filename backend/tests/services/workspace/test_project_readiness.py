# ruff: noqa: F811 -- fixture imported intentionally
"""Reject unsupported projects before minting deployment authority."""

import json
import uuid

import pytest
from sqlalchemy import delete, select

from app.models.workspace import WorkspaceDeployToken, WorkspaceFile, WorkspacePatch
from app.services.deploy_preview_service import (
    DeployGateNotMetError,
    compute_deploy_manifest,
    mint_deploy_token,
    verify_and_consume_deploy_token,
)
from tests.services.workspace.test_content_bound_deploy import checked_project, preview  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "CustomScripts/a.js",
        "FileCabinet/SuiteScripts/my script.js",
        "SuiteScripts/a.test.js",
        "SuiteScripts/a.spec.js",
        "SuiteScripts/__tests__/a.js",
        "SuiteScripts/__mocks__/a.js",
        "SuiteScripts/tests/a.js",
        "SuiteScripts/test/a.js",
        "FileCabinet/SuiteScripts/sub/EXAMPLE.TEST.js",
        "FileCabinet/SuiteScripts/sub/__snapshots__/a.js",
        "Objects/customscript_a.xml",
    ],
)
async def test_preview_rejects_unsupported_snapshot_before_token(db, checked_project, path):
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
    assert exc.value.gates["project"]["reason"] in {"unsupported_projection", "test_path_in_script_tree"}
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
