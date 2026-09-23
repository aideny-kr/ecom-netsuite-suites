"""One recon run gets one resolution agent at a time, however often it is dispatched.

The agent is dispatched both when a run completes (order_recon_job) and from the
plan-resolutions endpoint, so one run routinely got two concurrent tasks classifying
the same proposals: double model spend, duplicate shadow comparisons, and a summary
counting writes the compare-and-set in apply_agent_proposal had already refused.
Observed on staging 2026-09-22 (run 1ce568fd on uat-smoke).

Dispatches are SERIALIZED, not dropped: a dispatch that finds the run busy reschedules
itself, so work a re-plan created is processed by that re-plan's own dispatch once the
current task is done — never stranded, never classified twice.
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
    """Ask pg_locks, not pg_try_advisory_lock: advisory locks are reentrant per backend,
    so a probe handed the leader's own pooled connection would "acquire" a lock that
    was never released."""
    hi, lo = (key >> 32) & 0xFFFFFFFF, key & 0xFFFFFFFF
    async with db.bind.engine.connect() as probe:
        held = await probe.scalar(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND classid::bigint = :hi AND objid::bigint = :lo AND objsubid = 1 AND granted"
            ),
            {"hi": hi, "lo": lo},
        )
        await probe.commit()
        return held == 0


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
    assert summary["upgraded"] == 1 and summary["stopped"] == "done"
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


async def test_a_re_plan_during_the_run_leaves_its_proposals_for_its_own_dispatch(db, tenant_a, monkeypatch):
    """This task's batch is superseded mid-run: the compare-and-set refuses its writes,
    and the re-plan's fresh proposals stay eligible for the re-plan's own dispatch,
    which was rescheduled because this task held the run."""
    from app.services.reconciliation.resolution_agent import fetch_agent_eligible

    state = {"replanned": False}

    class ReplanOnFirstCall(FakeAdapter):
        async def create_message(self, **kwargs):
            if not state["replanned"]:
                state["replanned"] = True
                await plan_run(db, tenant_a.id, run.id)
            return await super().create_message(**kwargs)

    adapter = ReplanOnFirstCall(action="book_fee_line", narrative="Fee.")
    run, _, _ = await _setup(db, tenant_a.id, monkeypatch, n=2, adapter=adapter)
    summary = await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run.id))
    assert summary["not_applied"] == 2 and summary["upgraded"] == 0 and len(adapter.calls) == 2
    assert len(await fetch_agent_eligible(db, tenant_a.id, run.id)) == 2
    assert await _proposals(db, tenant_a.id, run.id, source="agent") == []


async def test_proposals_of_terminal_results_are_not_eligible(db, tenant_a, monkeypatch):
    """apply_agent_proposal refuses them, so fetching them only burned classifications
    and, oldest first under the 50-item cap, could starve every newer proposal."""
    from app.models.reconciliation import ReconciliationResult
    from app.services.reconciliation.resolution_agent import fetch_agent_eligible

    run, results, _ = await _setup(db, tenant_a.id, monkeypatch, n=2)
    (await db.get(ReconciliationResult, results[0].id)).status = "locked"
    await db.commit()
    eligible = await fetch_agent_eligible(db, tenant_a.id, run.id)
    assert [p.result_id for p in eligible] == [results[1].id]


async def test_a_failed_write_is_counted_not_retried(db, tenant_a, monkeypatch):
    from app.services.reconciliation import resolution_agent

    run, _, adapter = await _setup(db, tenant_a.id, monkeypatch, n=2)

    async def always_fails(*_args, **_kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(resolution_agent, "apply_agent_proposal", always_fails)
    summary = await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run.id))
    assert summary["persist_failures"] == 2 and len(adapter.calls) == 2


async def test_a_run_closed_mid_batch_gets_no_more_agent_proposals(db, tenant_a, monkeypatch):
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


@pytest.mark.parametrize("refusal", ["run_closed", "result_terminal", "compare_and_set_lost"])
async def test_a_refused_apply_ends_its_transaction(db, tenant_a, monkeypatch, refusal):
    """apply_agent_proposal reads the run FOR SHARE. Every way it can refuse must end
    the transaction too, or the lock outlives the call and a concurrent close_period
    waits behind the rest of the batch, LLM calls included."""
    from app.models.reconciliation import ReconciliationResult, ReconciliationRun
    from app.services.reconciliation.resolution_agent import apply_agent_proposal, fetch_agent_eligible

    run, results, _ = await _setup(db, tenant_a.id, monkeypatch)
    (proposal,) = await fetch_agent_eligible(db, tenant_a.id, run.id)
    out = {"action": "book_fee_line", "narrative": "Fee.", "key_evidence": []}
    if refusal == "run_closed":
        (await db.get(ReconciliationRun, run.id)).status = "closed"
    elif refusal == "result_terminal":
        (await db.get(ReconciliationResult, results[0].id)).status = "locked"
    else:
        (await db.get(ReconResolutionProposal, proposal.id)).status = "superseded"
    await db.commit()
    assert not db.in_transaction()
    assert await apply_agent_proposal(db, proposal, out) is False
    assert not db.in_transaction(), "the refusal left its FOR SHARE transaction open"


async def test_a_leader_whose_lock_connection_dies_stops(db, tenant_a, monkeypatch):
    """The session-level lock goes with its connection. A leader that lost it must stop,
    or a rescheduled dispatch could take the run and classify alongside it."""
    real_leader, leader_pid = agent_task._run_leader, {}

    @contextlib.asynccontextmanager
    async def recording_leader(session, key):
        async with real_leader(session, key) as leading:
            if leading:
                leader_pid["pid"] = await leading.backend_pid()
            yield leading

    class KillLeaderOnFirstCall(FakeAdapter):
        async def create_message(self, **kwargs):
            if not self.calls:
                await db.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": leader_pid["pid"]})
            return await super().create_message(**kwargs)

    adapter = KillLeaderOnFirstCall(action="book_fee_line", narrative="Fee.")
    run, _, _ = await _setup(db, tenant_a.id, monkeypatch, n=3, adapter=adapter)
    run_id = run.id
    monkeypatch.setattr(agent_task, "_run_leader", recording_leader)
    summary = await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run_id))
    assert summary["stopped"] == "leadership_lost"
    assert len(adapter.calls) == 1
    # the lock went DURING the item's classification: another task may own the run now,
    # so this one neither writes that item nor records it
    assert summary["processed"] == 0
    assert await _proposals(db, tenant_a.id, run_id, source="agent") == []
    assert await _lock_is_free(db, agent_task._run_lock_key(tenant_a.id, run.id))


# ── the Celery task: a busy run is rescheduled, never dropped ─────────────


@pytest.fixture
def task_run(monkeypatch):
    """recon_resolution_agent.run with the session and core stubbed; returns (run, sent)."""
    import uuid

    import app.core.database as database

    @contextlib.asynccontextmanager
    async def fake_session():
        yield object()

    async def no_context(_db, _tenant_id):
        return None

    monkeypatch.setattr(database, "worker_async_session", fake_session)
    monkeypatch.setattr(database, "set_tenant_context_session", no_context)
    sent = []
    monkeypatch.setattr(agent_task.celery_app, "send_task", lambda name, **kw: sent.append((name, kw)))
    ids = (str(uuid.uuid4()), str(uuid.uuid4()))

    def run(summary, **kwargs):
        async def fake_run(_db, _tenant_id, _run_id, job_id=None):
            return summary

        monkeypatch.setattr(agent_task, "run_resolution_agent", fake_run)
        return agent_task.recon_resolution_agent.run(*ids, **kwargs)

    return run, sent, ids


def test_a_busy_run_reschedules_the_dispatch_instead_of_dropping_it(task_run):
    run, sent, (tenant_id, run_id) = task_run
    result = run({"skipped": "already_running"})
    assert result == {"skipped": "already_running", "rescheduled_attempt": 1}
    ((name, kw),) = sent
    assert name == "tasks.recon_resolution_agent" and kw["queue"] == "recon"
    assert kw["countdown"] == agent_task.BUSY_RETRY_SECONDS
    assert kw["kwargs"] == {"tenant_id": tenant_id, "run_id": run_id, "busy_attempt": 1}


def test_a_run_busy_for_too_long_fails_the_job_visibly(task_run):
    run, sent, _ = task_run
    with pytest.raises(agent_task.ResolutionRunBusyError):
        run({"skipped": "already_running"}, busy_attempt=agent_task.MAX_BUSY_ATTEMPTS)
    assert sent == []


def test_a_reschedule_that_cannot_be_published_fails_the_job(task_run, monkeypatch):
    run, _, _ = task_run

    def broker_down(*_args, **_kwargs):
        raise ConnectionError("broker unavailable")

    monkeypatch.setattr(agent_task.celery_app, "send_task", broker_down)
    with pytest.raises(ConnectionError):
        run({"skipped": "already_running"})


def test_a_lost_leadership_fails_the_job(task_run):
    run, _, _ = task_run
    summary = {"processed": 1, "upgraded": 1, "persist_failures": 0, "stopped": "leadership_lost"}
    with pytest.raises(agent_task.ResolutionLeadershipLostError):
        run(summary)


async def test_a_lock_lost_during_a_shadow_classification_records_no_comparison(db, tenant_a, monkeypatch):
    """In shadow mode the item's Jev comparison is recorded after the write; a task that
    lost its lock during classification must record neither."""
    from app.services.reconciliation import resolution_jev
    from app.services.typesafe import audit

    real_leader, leader_pid = agent_task._run_leader, {}

    @contextlib.asynccontextmanager
    async def recording_leader(session, key):
        async with real_leader(session, key) as leading:
            if leading:
                leader_pid["pid"] = await leading.backend_pid()
            yield leading

    async def shadow_decision(*_args, **_kwargs):
        await db.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": leader_pid["pid"]})
        validated = {"action": "book_fee_line", "narrative": "Fee.", "key_evidence": []}
        return validated, {"llm_action": "book_fee_line", "jev_action": "book_fee_line"}

    recorded = []

    async def spy(*args, **kwargs):
        recorded.append(kwargs)

    run, _, _ = await _setup(db, tenant_a.id, monkeypatch, n=2)
    run_id = run.id
    monkeypatch.setattr(agent_task, "_run_leader", recording_leader)
    monkeypatch.setattr(resolution_jev, "decide_item", shadow_decision)
    monkeypatch.setattr(audit, "record_comparison", spy)
    summary = await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run_id))
    assert summary["stopped"] == "leadership_lost" and summary["processed"] == 0
    assert recorded == []
    assert await _proposals(db, tenant_a.id, run_id, source="agent") == []


async def test_a_tenant_context_failure_after_a_failed_write_stops_the_run(db, tenant_a, monkeypatch):
    """After a failed write the next transaction re-applies the tenant (after_begin). If
    that fails, the run must stop loudly, not be counted as a shadow-comparison failure
    by the comparison's own except block and carry on."""
    from app.core import database
    from app.core.database import set_tenant_context_session
    from app.services.reconciliation import resolution_agent, resolution_jev

    run, _, _ = await _setup(db, tenant_a.id, monkeypatch, n=1)
    await set_tenant_context_session(db, str(tenant_a.id))

    async def shadow_decision(*_args, **_kwargs):
        return {"action": "book_fee_line", "narrative": "Fee.", "key_evidence": []}, {"llm_action": "book_fee_line"}

    async def fails_and_breaks_the_context(*_args, **_kwargs):
        monkeypatch.setattr(database, "_SET_TENANT_LOCAL", text("SELECT 1/0"))
        raise RuntimeError("connection reset")

    monkeypatch.setattr(resolution_jev, "decide_item", shadow_decision)
    monkeypatch.setattr(resolution_agent, "apply_agent_proposal", fails_and_breaks_the_context)
    with pytest.raises(Exception, match="division by zero"):
        await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run.id))
