"""One recon run gets one resolution agent, however many times it is dispatched.

The agent is dispatched both when a run completes (order_recon_job) and from the
plan-resolutions endpoint, so one run routinely got two concurrent tasks classifying
the same proposals: double model spend, duplicate shadow comparisons, and a summary
counting writes the compare-and-set in apply_agent_proposal had already refused.
Observed on staging 2026-09-22 (run 1ce568fd on uat-smoke).
"""

import asyncio
import contextlib
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.core.config import settings
from app.models.reconciliation import ReconResolutionProposal
from app.services.reconciliation.resolution_planner import plan_run
from app.workers.tasks import recon_resolution_agent as agent_task
from tests.conftest import create_test_recon_result, create_test_recon_run, enable_feature_flag
from tests.test_resolution_agent_task import FakeAdapter


async def _result(db, tenant_id, run_id, i):
    return await create_test_recon_result(
        db, tenant_id, run_id, status="pending", bucket="needs_review", match_type="deterministic",
        variance_type="manual_adjustment", variance_amount=Decimal("77.10"), stripe_amount=Decimal("500.00"),
        netsuite_amount=Decimal("422.90"), evidence={"charge_source_id": f"ch_sf{i}", "order_reference": f"R7000000{i}"},
    )  # fmt: skip


async def _setup(db, tenant_id, monkeypatch, n=1, adapter=None):
    await enable_feature_flag(db, tenant_id, "reconciliation")
    await enable_feature_flag(db, tenant_id, "recon_resolution_agent")
    run = await create_test_recon_run(db, tenant_id, status="completed")
    results = [await _result(db, tenant_id, run.id, i) for i in range(n)]
    await db.flush()
    await plan_run(db, tenant_id, run.id)
    adapter = adapter or FakeAdapter(action="book_fee_line", narrative="Fee.")

    async def fake_config(_db, _tenant_id):
        return ("anthropic", "test-model", "sk-test", False)

    monkeypatch.setattr(agent_task, "get_adapter", lambda provider, api_key: adapter)
    monkeypatch.setattr(agent_task, "get_tenant_ai_config", fake_config)
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "off")
    return run, results, adapter


async def _proposals(db, tenant_id, run_id, **where):
    P = ReconResolutionProposal
    stmt = select(P).where(P.tenant_id == tenant_id, P.run_id == run_id)
    for column, value in where.items():
        stmt = stmt.where(getattr(P, column) == value)
    return list((await db.execute(stmt)).scalars().all())


@contextlib.asynccontextmanager
async def _held_elsewhere(db, key):
    """The run's lock, held by another connection — i.e. another worker's task."""
    async with db.bind.engine.connect() as other:
        await other.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
        await other.commit()
        try:
            yield
        finally:
            await other.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            await other.commit()


async def _lock_is_free(db, key) -> bool:
    async with db.bind.engine.connect() as probe:
        got = await probe.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        if got:
            await probe.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        await probe.commit()
        return bool(got)


async def test_a_dispatch_for_a_run_another_task_holds_classifies_nothing(db, tenant_a, monkeypatch):
    run, _, adapter = await _setup(db, tenant_a.id, monkeypatch)
    key = agent_task._run_lock_key(tenant_a.id, run.id)
    async with _held_elsewhere(db, key):
        summary = await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run.id))
    assert summary == {"skipped": "already_running"}
    assert adapter.calls == []
    assert len(await _proposals(db, tenant_a.id, run.id, source="agent")) == 0


async def test_the_lock_is_free_after_a_run(db, tenant_a, monkeypatch):
    run, _, _ = await _setup(db, tenant_a.id, monkeypatch)
    summary = await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run.id))
    assert summary["upgraded"] == 1 and summary["stopped"] == "drained"
    assert await _lock_is_free(db, agent_task._run_lock_key(tenant_a.id, run.id))


async def test_the_lock_is_free_after_a_cancelled_run(db, tenant_a, monkeypatch):
    from app.services.reconciliation import resolution_jev

    run, _, _ = await _setup(db, tenant_a.id, monkeypatch)

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(resolution_jev, "decide_item", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run.id))
    assert await _lock_is_free(db, agent_task._run_lock_key(tenant_a.id, run.id))


async def test_a_re_plan_during_the_run_is_drained_with_one_agent_row_per_result(db, tenant_a, monkeypatch):
    """A re-plan supersedes the proposals the task already fetched and creates new ones.
    The first writes are refused by the compare-and-set; the leader must go on to the new
    proposals itself, because the re-plan's own dispatch now finds the run taken."""
    state = {"replanned": False}

    class ReplanOnFirstCall(FakeAdapter):
        async def create_message(self, **kwargs):
            if not state["replanned"]:
                state["replanned"] = True
                await plan_run(db, tenant_a.id, run.id)
            return await super().create_message(**kwargs)

    adapter = ReplanOnFirstCall(action="book_fee_line", narrative="Fee.")
    run, results, _ = await _setup(db, tenant_a.id, monkeypatch, n=2, adapter=adapter)
    summary = await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run.id))

    agent_rows = await _proposals(db, tenant_a.id, run.id, source="agent", status="proposed")
    assert sorted(str(r.result_id) for r in agent_rows) == sorted(str(r.id) for r in results)
    assert summary["not_applied"] == 2 and summary["upgraded"] == 2
    assert summary["stopped"] == "drained"
    assert len(adapter.calls) == 4


async def test_proposals_arriving_after_the_last_fetch_but_before_the_unlock_are_picked_up(db, tenant_a, monkeypatch):
    """The gap between the leader's last empty fetch and its unlock: a dispatch landing
    there finds the lock held and skips, so the leader re-checks after unlocking."""
    run, _, adapter = await _setup(db, tenant_a.id, monkeypatch)
    real_leader, state = agent_task._run_leader, {"exits": 0, "late": None}

    @contextlib.asynccontextmanager
    async def leader_with_late_arrival(session, key):
        async with real_leader(session, key) as leading:
            yield leading
            state["exits"] += 1
            if leading and state["exits"] == 1:  # drained, lock still held
                state["late"] = await _result(db, tenant_a.id, run.id, 9)
                await db.flush()
                await plan_run(db, tenant_a.id, run.id)

    monkeypatch.setattr(agent_task, "_run_leader", leader_with_late_arrival)
    summary = await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run.id))

    late = await _proposals(db, tenant_a.id, run.id, source="agent", result_id=state["late"].id)
    assert len(late) == 1, "the late proposal was stranded"
    assert summary["stopped"] == "drained" and state["exits"] == 2


async def test_a_failed_write_is_never_retried_in_the_drain(db, tenant_a, monkeypatch):
    from app.services.reconciliation import resolution_agent

    run, _, adapter = await _setup(db, tenant_a.id, monkeypatch, n=2)

    async def always_fails(*_args, **_kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(resolution_agent, "apply_agent_proposal", always_fails)
    summary = await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run.id))
    assert summary["persist_failures"] == 2 and len(adapter.calls) == 2
    assert summary["stopped"] == "drained"


async def test_one_task_classifies_at_most_the_per_run_budget(db, tenant_a, monkeypatch):
    """Draining must not raise the cost bound: a task makes at most MAX_ITEMS_PER_RUN
    classifications across all its rounds, however many new proposals keep arriving."""
    from app.services.reconciliation import resolution_agent

    monkeypatch.setattr(resolution_agent, "MAX_ITEMS_PER_RUN", 3)

    class ReplanEveryCall(FakeAdapter):
        async def create_message(self, **kwargs):
            await plan_run(db, tenant_a.id, run.id)
            return await super().create_message(**kwargs)

    adapter = ReplanEveryCall(action="book_fee_line", narrative="Fee.")
    run, _, _ = await _setup(db, tenant_a.id, monkeypatch, adapter=adapter)
    summary = await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run.id))
    assert summary["stopped"] == "budget"
    assert len(adapter.calls) == 3
    assert await _lock_is_free(db, agent_task._run_lock_key(tenant_a.id, run.id))


async def test_a_run_closed_mid_drain_gets_no_more_agent_proposals(db, tenant_a, monkeypatch):
    """Close is a hard freeze, and close_period leaves needs-review results unlocked, so
    the result-status guard alone cannot stop the agent writing into a closed run."""
    from app.models.reconciliation import ReconciliationRun

    class CloseOnFirstCall(FakeAdapter):
        async def create_message(self, **kwargs):
            if not self.calls:
                (await db.get(ReconciliationRun, run.id)).status = "closed"
                await db.commit()
            return await super().create_message(**kwargs)

    adapter = CloseOnFirstCall(action="book_fee_line", narrative="Fee.")
    run, _, _ = await _setup(db, tenant_a.id, monkeypatch, n=3, adapter=adapter)
    run_id = run.id
    summary = await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run_id))
    assert summary["stopped"] == "run_closed"
    assert len(adapter.calls) == 1  # nothing classified after the close
    assert summary["not_applied"] == 1 and summary["upgraded"] == 0
    assert await _proposals(db, tenant_a.id, run_id, source="agent") == []


@pytest.mark.parametrize("status", ["closed", "locked"])
async def test_apply_refuses_to_write_into_a_closed_run(db, tenant_a, monkeypatch, status):
    from app.models.reconciliation import ReconciliationRun
    from app.services.reconciliation.resolution_agent import apply_agent_proposal, fetch_agent_eligible

    run, _, _ = await _setup(db, tenant_a.id, monkeypatch)
    (proposal,) = await fetch_agent_eligible(db, tenant_a.id, run.id)
    (await db.get(ReconciliationRun, run.id)).status = status
    await db.commit()
    out = {"action": "book_fee_line", "narrative": "Fee.", "key_evidence": []}
    assert await apply_agent_proposal(db, proposal, out) is False
    assert await _proposals(db, tenant_a.id, run.id, source="agent") == []
    assert (await db.get(ReconResolutionProposal, proposal.id)).status == "proposed"
