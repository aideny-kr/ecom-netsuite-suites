"""Deploy gate freshness: snapshot-hash lookup + suitecloud_validate switch.

Covers Task 9 of the Validate UX plan:

* Validate gate now reads `run_type="suitecloud_validate"` (the legacy
  `sdf_validate` run type must NOT count as a passing validate).
* Optional `expected_snapshot_hash` parameter — when provided, the latest
  validate's `snapshot_hash` must match; mismatch → status="stale", fresh=False,
  deploy blocked.
* Validate gate reads `gate_status` (pass/block) instead of bare `status`
  to determine pass/fail. A run with status="failed" + gate_status="block"
  must block deploy.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace import Workspace, WorkspaceChangeSet, WorkspaceRun
from app.services import deploy_service


@pytest_asyncio.fixture
async def seeded_workspace_with_changeset(db: AsyncSession, tenant_a, admin_user):
    user, _ = admin_user
    workspace = Workspace(
        tenant_id=tenant_a.id,
        name="ws-deploy-gate",
        description=None,
        status="active",
        created_by=user.id,
    )
    db.add(workspace)
    await db.flush()
    cs = WorkspaceChangeSet(
        tenant_id=tenant_a.id,
        workspace_id=workspace.id,
        title="Deploy CS",
        status="approved",
        proposed_by=user.id,
    )
    db.add(cs)
    await db.flush()
    return workspace, cs, user


@pytest.mark.asyncio
async def test_deploy_uses_suitecloud_validate_run_type(seeded_workspace_with_changeset, db: AsyncSession) -> None:
    """Legacy sdf_validate runs MUST NOT count as a passing validate."""
    workspace, cs, admin = seeded_workspace_with_changeset
    legacy = WorkspaceRun(
        tenant_id=workspace.tenant_id,
        workspace_id=workspace.id,
        changeset_id=cs.id,
        run_type="sdf_validate",
        status="passed",
        triggered_by=admin.id,
    )
    db.add(legacy)
    await db.flush()
    gates = await deploy_service.check_deploy_prerequisites(
        db=db,
        changeset_id=cs.id,
        tenant_id=workspace.tenant_id,
    )
    assert gates["gates"]["validate"]["status"] == "missing"
    assert gates["allowed"] is False  # validate missing


@pytest.mark.asyncio
async def test_fresh_validate_with_matching_hash_passes_gate(seeded_workspace_with_changeset, db: AsyncSession) -> None:
    workspace, cs, admin = seeded_workspace_with_changeset
    run = WorkspaceRun(
        tenant_id=workspace.tenant_id,
        workspace_id=workspace.id,
        changeset_id=cs.id,
        run_type="suitecloud_validate",
        status="passed",
        gate_status="pass",
        snapshot_hash="a" * 64,
        triggered_by=admin.id,
    )
    db.add(run)
    test_run = WorkspaceRun(
        tenant_id=workspace.tenant_id,
        workspace_id=workspace.id,
        changeset_id=cs.id,
        run_type="jest_unit_test",
        status="passed",
        triggered_by=admin.id,
    )
    db.add(test_run)
    await db.flush()
    gates = await deploy_service.check_deploy_prerequisites(
        db=db,
        changeset_id=cs.id,
        tenant_id=workspace.tenant_id,
        expected_snapshot_hash="a" * 64,
    )
    assert gates["gates"]["validate"]["status"] == "passed"
    assert gates["gates"]["validate"]["fresh"] is True
    assert gates["allowed"] is True


@pytest.mark.asyncio
async def test_stale_snapshot_hash_marks_validate_stale(seeded_workspace_with_changeset, db: AsyncSession) -> None:
    workspace, cs, admin = seeded_workspace_with_changeset
    run = WorkspaceRun(
        tenant_id=workspace.tenant_id,
        workspace_id=workspace.id,
        changeset_id=cs.id,
        run_type="suitecloud_validate",
        status="passed",
        gate_status="pass",
        snapshot_hash="a" * 64,
        triggered_by=admin.id,
    )
    db.add(run)
    await db.flush()
    gates = await deploy_service.check_deploy_prerequisites(
        db=db,
        changeset_id=cs.id,
        tenant_id=workspace.tenant_id,
        expected_snapshot_hash="b" * 64,  # mismatch
    )
    assert gates["gates"]["validate"]["status"] == "stale"
    assert gates["gates"]["validate"]["fresh"] is False
    assert gates["allowed"] is False


@pytest.mark.asyncio
async def test_validate_with_block_gate_status_blocks_deploy(seeded_workspace_with_changeset, db: AsyncSession) -> None:
    """gate_status=block (errors) must block deploy even if status='failed'."""
    workspace, cs, admin = seeded_workspace_with_changeset
    run = WorkspaceRun(
        tenant_id=workspace.tenant_id,
        workspace_id=workspace.id,
        changeset_id=cs.id,
        run_type="suitecloud_validate",
        status="failed",
        gate_status="block",
        snapshot_hash="a" * 64,
        has_errors=True,
        triggered_by=admin.id,
    )
    db.add(run)
    await db.flush()
    gates = await deploy_service.check_deploy_prerequisites(
        db=db,
        changeset_id=cs.id,
        tenant_id=workspace.tenant_id,
    )
    assert gates["gates"]["validate"]["status"] != "passed"
    assert gates["allowed"] is False
