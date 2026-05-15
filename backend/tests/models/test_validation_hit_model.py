"""Validation-hit model + migration tests."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import inspect as sqla_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace import ValidationHit, Workspace, WorkspaceRun


@pytest_asyncio.fixture
async def seeded_workspace(db: AsyncSession, tenant_a, admin_user) -> Workspace:
    """Create a minimal Workspace owned by tenant_a + admin_user for the model test."""
    user, _ = admin_user
    ws = Workspace(
        tenant_id=tenant_a.id,
        name="Validate Hit Test Workspace",
        created_by=user.id,
        status="active",
    )
    db.add(ws)
    await db.flush()
    return ws


@pytest.mark.asyncio
async def test_validation_hit_persists_with_run(db: AsyncSession, seeded_workspace: Workspace) -> None:
    run = WorkspaceRun(
        tenant_id=seeded_workspace.tenant_id,
        workspace_id=seeded_workspace.id,
        run_type="suitecloud_validate",
        status="failed",
        triggered_by=seeded_workspace.created_by,
        validator_engine="suitecloud_server",
        parser_version="1.0.0",
        has_errors=True,
        has_warnings=False,
        gate_status="block",
        snapshot_hash="a" * 64,
    )
    db.add(run)
    await db.flush()

    hit = ValidationHit(
        tenant_id=seeded_workspace.tenant_id,
        run_id=run.id,
        file_path="src/Suitelets/foo.js",
        line=42,
        severity="error",
        code="OWASP-A03",
        rule_id="netsuite-owasp-secure-coding/injection",
        message="Unsanitized user input flowed into N/query",
        fingerprint="0123456789abcdef" * 4,
    )
    db.add(hit)
    await db.flush()

    fetched = (await db.execute(select(ValidationHit).where(ValidationHit.run_id == run.id))).scalar_one()
    assert fetched.severity == "error"
    assert fetched.fingerprint == "0123456789abcdef" * 4
    assert fetched.code == "OWASP-A03"
    assert fetched.tenant_id == seeded_workspace.tenant_id
    assert isinstance(fetched.id, uuid.UUID)


@pytest.mark.asyncio
async def test_run_gate_status_columns_exist(db: AsyncSession) -> None:
    """The 074 migration must add the six gate-status columns to workspace_runs."""

    def _check(sync_conn) -> None:
        cols = {c["name"] for c in sqla_inspect(sync_conn).get_columns("workspace_runs")}
        assert {
            "validator_engine",
            "parser_version",
            "has_errors",
            "has_warnings",
            "gate_status",
            "snapshot_hash",
        }.issubset(cols), f"missing one or more validate columns; got: {cols}"

    conn = await db.connection()
    await conn.run_sync(_check)
