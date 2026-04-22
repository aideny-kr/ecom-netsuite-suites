"""Tests for AgentLabRun model — ORM shape + partial unique index behaviour."""

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_lab_run import AgentLabRun
from app.models.tenant import Tenant


class TestAgentLabRunModel:
    def test_model_has_expected_columns(self):
        columns = {c.name for c in AgentLabRun.__table__.columns}
        required = {
            "id", "tenant_id", "triggered_by_user_id", "kind", "mode",
            "case_id", "status", "total_cases", "cases_completed",
            "cost_usd_actual", "started_at", "finished_at", "error_message",
            "created_at", "updated_at",
        }
        assert required.issubset(columns), f"Missing: {required - columns}"

    def test_tablename(self):
        assert AgentLabRun.__tablename__ == "agent_lab_runs"

    def test_status_server_default(self):
        status_col = AgentLabRun.__table__.columns["status"]
        assert status_col.server_default is not None
        assert "running" in str(status_col.server_default.arg)


@pytest.mark.asyncio
async def test_partial_unique_index_blocks_same_kind_running(
    db: AsyncSession,
    tenant_a: Tenant,
):
    """Two rows with status=running + same (tenant_id, kind) violate the partial unique index."""
    run_a = AgentLabRun(
        tenant_id=tenant_a.id,
        kind="benchmark",
        mode="all",
        status="running",
        total_cases=18,
    )
    db.add(run_a)
    await db.flush()

    run_b = AgentLabRun(
        tenant_id=tenant_a.id,
        kind="benchmark",
        mode="all",
        status="running",
        total_cases=18,
    )
    db.add(run_b)
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


@pytest.mark.asyncio
async def test_partial_unique_index_allows_different_kinds(db: AsyncSession, tenant_a: Tenant):
    """Same tenant can have benchmark + experiment running concurrently."""
    db.add(AgentLabRun(
        tenant_id=tenant_a.id, kind="benchmark", mode="all",
        status="running", total_cases=18,
    ))
    db.add(AgentLabRun(
        tenant_id=tenant_a.id, kind="experiment", mode="all",
        status="running", total_cases=60,
    ))
    await db.flush()  # should not raise


@pytest.mark.asyncio
async def test_partial_unique_index_allows_completed_to_rerun(db: AsyncSession, tenant_a: Tenant):
    """A completed run doesn't block a new running one of the same kind."""
    db.add(AgentLabRun(
        tenant_id=tenant_a.id, kind="benchmark", mode="all",
        status="completed", total_cases=18, cases_completed=18,
    ))
    db.add(AgentLabRun(
        tenant_id=tenant_a.id, kind="benchmark", mode="all",
        status="running", total_cases=18,
    ))
    await db.flush()  # should not raise
