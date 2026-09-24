"""Worker-level proof for A1: the comparison is recorded, and who decided is honoured."""

import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.audit import AuditEvent
from app.models.reconciliation import ReconResolutionProposal
from app.services.reconciliation import resolution_jev as rj
from app.services.reconciliation.resolution_agent import AGENT_ALLOWED_ACTIONS
from app.services.typesafe.client import JevResult
from app.workers.tasks import recon_resolution_agent as agent_task
from tests.conftest import enable_feature_flag
from tests.resolution_evidence_helpers import seed_run_linked_evidence
from tests.test_resolution_agent_task import FakeAdapter, _seed_planned_run


def _jev_answer(action, confidence):
    probabilities = {a: 0.0 for a in AGENT_ALLOWED_ACTIONS}
    probabilities[action] = 1.0
    answers = {"action": {"type": "choice", "choice": action, "probabilities": probabilities, "confidence": confidence}}
    return JevResult(answers=answers, model="jev-1.13.0", input_tokens=300, elapsed_ms=80)


@pytest.fixture(autouse=True)
def _platform_key(monkeypatch):
    # Jev needs a key; the mode each test sets is the deployment cap (typesafe.access).
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", "platform-test-key")


async def _run(db, tenant, monkeypatch, *, mode, jev_action, jev_confidence):
    await enable_feature_flag(db, tenant.id, "reconciliation")
    await enable_feature_flag(db, tenant.id, "recon_resolution_agent")
    run, _ = await _seed_planned_run(db, tenant.id)
    adapter = FakeAdapter(action="book_fee_line", narrative="Book the difference as a fee line.")

    async def fake_config(_db, _tenant_id):
        return ("anthropic", "test-model", "sk-test", False)

    async def fake_try_ask(tenant_id, state=None, questions=None, *, build=None, **_):
        return _jev_answer(jev_action, jev_confidence), None

    monkeypatch.setattr(agent_task, "get_adapter", lambda provider, api_key: adapter)
    monkeypatch.setattr(agent_task, "get_tenant_ai_config", fake_config)
    monkeypatch.setattr(rj, "try_ask", fake_try_ask)
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", mode)

    await agent_task.run_resolution_agent(db, str(tenant.id), str(run.id))

    agent_rows = (
        (
            await db.execute(
                select(ReconResolutionProposal).where(
                    ReconResolutionProposal.run_id == run.id, ReconResolutionProposal.source == "agent"
                )
            )
        )
        .scalars()
        .all()
    )
    events = (
        (
            await db.execute(
                select(AuditEvent).where(AuditEvent.tenant_id == tenant.id, AuditEvent.action == "recon.jev_comparison")
            )
        )
        .scalars()
        .all()
    )
    return adapter, agent_rows, events


async def test_shadow_applies_the_llm_decision_and_records_the_comparison(db, tenant_a, monkeypatch):
    adapter, rows, events = await _run(
        db, tenant_a, monkeypatch, mode="shadow", jev_action="carry_forward", jev_confidence=0.97
    )
    assert [r.action for r in rows] == ["book_fee_line"]
    assert len(adapter.calls) == 1
    assert len(events) == 1
    payload = events[0].payload
    assert payload["decided_by"] == "llm" and payload["agree"] is False
    assert payload["jev_action"] == "carry_forward" and payload["llm_action"] == "book_fee_line"
    # Decisions and timings only: no tenant text leaves through the audit payload.
    assert set(payload) <= {
        "mode", "jev_action", "jev_confidence", "jev_probabilities", "jev_elapsed_ms", "jev_error",
        "jev_model", "llm_action", "llm_elapsed_ms", "agree", "decided_by", "guard_veto", "applied_action",
        "jev_validated_action", "jev_veto", "eligibility_veto", "llm_error", "applied",
    }  # fmt: skip


async def test_live_confident_but_ineligible_jev_pick_uses_guarded_fallback(db, tenant_a, monkeypatch):
    """The seeded item is a manual_adjustment with no fee, timing, washout or recency fact,
    so carry_forward has no factual basis; the gate abstains and records why."""
    adapter, rows, events = await _run(
        db, tenant_a, monkeypatch, mode="live", jev_action="carry_forward", jev_confidence=0.97
    )
    assert [r.action for r in rows] == ["book_fee_line"]
    assert len(adapter.calls) == 1
    payload = events[0].payload
    assert payload["decided_by"] == "llm" and payload["eligibility_veto"] == "carry_forward"
    assert payload["applied"] is True
    assert not any(ch.isdigit() for ch in rows[0].narrative)


async def test_off_records_nothing(db, tenant_a, monkeypatch):
    _, rows, events = await _run(db, tenant_a, monkeypatch, mode="off", jev_action="carry_forward", jev_confidence=0.97)
    assert [r.action for r in rows] == ["book_fee_line"]
    assert events == []


async def test_the_comparison_survives_when_the_proposal_cannot_be_applied(db, tenant_a, monkeypatch):
    """apply_agent_proposal returns early (no commit) when the planner row was already
    decided; the comparison row must not ride on that commit."""
    from sqlalchemy import update

    await enable_feature_flag(db, tenant_a.id, "reconciliation")
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_agent")
    run, _ = await _seed_planned_run(db, tenant_a.id)
    adapter = FakeAdapter(action="book_fee_line", narrative="Fee.")

    async def fake_config(_db, _tenant_id):
        return ("anthropic", "test-model", "sk-test", False)

    async def fake_try_ask(tenant_id, state=None, questions=None, *, build=None, **_):
        return _jev_answer("carry_forward", 0.9), None

    monkeypatch.setattr(agent_task, "get_adapter", lambda provider, api_key: adapter)
    monkeypatch.setattr(agent_task, "get_tenant_ai_config", fake_config)
    monkeypatch.setattr(rj, "try_ask", fake_try_ask)
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "shadow")

    from app.services.reconciliation import resolution_agent as ra

    original_apply = ra.apply_agent_proposal

    async def supersede_then_apply(db_, item, validated):
        # a human decides the planner row between planning and the agent tail
        await db_.execute(
            update(ReconResolutionProposal).where(ReconResolutionProposal.id == item.id).values(status="superseded")
        )
        return await original_apply(db_, item, validated)

    monkeypatch.setattr(ra, "apply_agent_proposal", supersede_then_apply)

    # The test session sees its own uncommitted rows, so presence alone proves nothing.
    # Spy on the order: the comparison must be followed by a commit the worker owns.
    from app.services.typesafe import audit as jev_audit

    order = []
    real_record, real_commit = jev_audit.record_comparison, db.commit

    async def spy_record(*a, **k):
        order.append("record")
        return await real_record(*a, **k)

    async def spy_commit():
        order.append("commit")
        return await real_commit()

    monkeypatch.setattr(jev_audit, "record_comparison", spy_record)
    monkeypatch.setattr(db, "commit", spy_commit)
    await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run.id))
    assert "record" in order and order[order.index("record") + 1] == "commit", order

    events = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.tenant_id == tenant_a.id, AuditEvent.action == "recon.jev_comparison"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1 and events[0].payload["jev_action"] == "carry_forward"


async def test_a_persistence_failure_on_one_item_does_not_abort_the_run(db, tenant_a, monkeypatch):
    await enable_feature_flag(db, tenant_a.id, "reconciliation")
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_agent")
    run, _ = await _seed_planned_run(db, tenant_a.id)
    adapter = FakeAdapter(action="book_fee_line", narrative="Fee.")

    async def fake_config(_db, _tenant_id):
        return ("anthropic", "test-model", "sk-test", False)

    async def fake_try_ask(tenant_id, state=None, questions=None, *, build=None, **_):
        return _jev_answer("carry_forward", 0.9), None

    monkeypatch.setattr(agent_task, "get_adapter", lambda provider, api_key: adapter)
    monkeypatch.setattr(agent_task, "get_tenant_ai_config", fake_config)
    monkeypatch.setattr(rj, "try_ask", fake_try_ask)
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "shadow")

    real_commit = db.commit
    state = {"raised": False}

    async def failing_commit():
        if not state["raised"]:
            state["raised"] = True
            raise RuntimeError("connection reset")
        return await real_commit()

    monkeypatch.setattr(db, "commit", failing_commit)
    summary = await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run.id))  # must not raise
    assert summary["persist_failures"] == 1 and summary["processed"] == 1


async def test_a_failed_comparison_commit_after_a_successful_apply_counts_the_item_as_applied(
    db, tenant_a, monkeypatch
):
    """apply_agent_proposal commits its own write; a later failure recording the
    comparison must not turn a durably applied item into a 'persist failure'."""
    await enable_feature_flag(db, tenant_a.id, "reconciliation")
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_agent")
    run, _ = await _seed_planned_run(db, tenant_a.id)
    adapter = FakeAdapter(action="book_fee_line", narrative="Fee.")

    async def fake_config(_db, _tenant_id):
        return ("anthropic", "test-model", "sk-test", False)

    async def fake_try_ask(tenant_id, state=None, questions=None, *, build=None, **_):
        return _jev_answer("carry_forward", 0.9), None

    monkeypatch.setattr(agent_task, "get_adapter", lambda provider, api_key: adapter)
    monkeypatch.setattr(agent_task, "get_tenant_ai_config", fake_config)
    monkeypatch.setattr(rj, "try_ask", fake_try_ask)
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "shadow")

    real_commit = db.commit
    calls = {"n": 0}

    async def second_commit_fails():
        calls["n"] += 1
        if calls["n"] == 2:  # 1st = apply's own commit, 2nd = the comparison's
            raise RuntimeError("connection reset")
        return await real_commit()

    monkeypatch.setattr(db, "commit", second_commit_fails)
    run_id, tenant_id = run.id, tenant_a.id  # the worker's rollback expires these instances
    summary = await agent_task.run_resolution_agent(db, str(tenant_id), str(run_id))
    assert summary["upgraded"] == 1 and summary["persist_failures"] == 0 and summary["comparison_failures"] == 1
    rows = (
        (
            await db.execute(
                select(ReconResolutionProposal).where(
                    ReconResolutionProposal.run_id == run_id, ReconResolutionProposal.source == "agent"
                )
            )
        )
        .scalars()
        .all()
    )
    assert [r.action for r in rows] == ["book_fee_line"]


async def test_a_rollback_on_one_item_does_not_cascade_into_the_next(db, tenant_a, monkeypatch):
    """Rollback expires every loaded instance; the second item must still be classified
    and applied, not degraded by lazy IO on an expired object."""
    from decimal import Decimal

    from app.services.reconciliation.resolution_planner import plan_run
    from tests.conftest import create_test_recon_result, create_test_recon_run

    await enable_feature_flag(db, tenant_a.id, "reconciliation")
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_agent")
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    for i in range(2):
        await create_test_recon_result(
            db, tenant_a.id, run.id, status="pending", bucket="needs_review", match_type="deterministic",
            variance_type="manual_adjustment", variance_amount=Decimal("77.10"), stripe_amount=Decimal("500.00"),
            netsuite_amount=Decimal("422.90"), evidence={"charge_source_id": f"ch_{i}", "order_reference": f"R62848927{i}"},
        )  # fmt: skip
    await db.flush()
    await seed_run_linked_evidence(db, tenant_a.id, run.id)
    await plan_run(db, tenant_a.id, run.id)
    adapter = FakeAdapter(action="book_fee_line", narrative="Fee.")

    async def fake_config(_db, _tenant_id):
        return ("anthropic", "test-model", "sk-test", False)

    monkeypatch.setattr(agent_task, "get_adapter", lambda provider, api_key: adapter)
    monkeypatch.setattr(agent_task, "get_tenant_ai_config", fake_config)
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "off")

    real_commit = db.commit
    state = {"raised": False}

    async def first_commit_fails():
        if not state["raised"]:
            state["raised"] = True
            raise RuntimeError("connection reset")
        return await real_commit()

    monkeypatch.setattr(db, "commit", first_commit_fails)
    summary = await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run.id))
    assert summary["processed"] == 2 and summary["persist_failures"] == 1
    assert summary["upgraded"] == 1 and len(adapter.calls) == 2


# ── gate round 4 on #285 ───────────────────────────────────────────────────


async def test_tenant_context_survives_a_rollback_before_any_commit(db, tenant_a, monkeypatch):
    """A plain SET is undone when the transaction it ran in rolls back. If the first
    item fails before anything has committed, every later item must still run under
    the tenant's context."""
    from decimal import Decimal

    from sqlalchemy import text

    from app.core.database import set_tenant_context_session
    from app.services.reconciliation.resolution_planner import plan_run
    from tests.conftest import create_test_recon_result, create_test_recon_run

    await enable_feature_flag(db, tenant_a.id, "reconciliation")
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_agent")
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    for i in range(2):
        await create_test_recon_result(
            db, tenant_a.id, run.id, status="pending", bucket="needs_review", match_type="deterministic",
            variance_type="manual_adjustment", variance_amount=Decimal("77.10"), stripe_amount=Decimal("500.00"),
            netsuite_amount=Decimal("422.90"), evidence={"charge_source_id": f"ch_{i}", "order_reference": f"R62848927{i}"},
        )  # fmt: skip
    await db.flush()
    await seed_run_linked_evidence(db, tenant_a.id, run.id)
    await plan_run(db, tenant_a.id, run.id)
    adapter = FakeAdapter(action="book_fee_line", narrative="Fee.")

    async def fake_config(_db, _tenant_id):
        return ("anthropic", "test-model", "sk-test", False)

    monkeypatch.setattr(agent_task, "get_adapter", lambda provider, api_key: adapter)
    monkeypatch.setattr(agent_task, "get_tenant_ai_config", fake_config)
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "off")

    from app.services.reconciliation import resolution_agent

    tenant_id = str(tenant_a.id)
    await set_tenant_context_session(db, tenant_id)
    seen, real_apply = [], resolution_agent.apply_agent_proposal

    async def first_apply_fails(session, item, validated):
        seen.append((await session.execute(text("SELECT current_setting('app.current_tenant_id', true)"))).scalar())
        if len(seen) == 1:
            raise RuntimeError("connection reset")
        return await real_apply(session, item, validated)

    monkeypatch.setattr(resolution_agent, "apply_agent_proposal", first_apply_fails)
    summary = await agent_task.run_resolution_agent(db, tenant_id, str(run.id))
    assert summary["persist_failures"] == 1 and summary["upgraded"] == 1
    assert seen == [tenant_id, tenant_id]


def test_a_run_with_an_unpersisted_item_fails_its_job(monkeypatch):
    """Every item is still tried, but the job must read `failed`, not `completed`, when
    any proposal was not written: absence is not success."""
    import contextlib

    import app.core.database as database

    disposed = []

    @contextlib.asynccontextmanager
    async def fake_session():
        yield object()
        disposed.append(True)  # like worker_async_session: skipped when an exception unwinds through it

    async def no_context(_db, _tenant_id):
        return None

    def summary_with(failures):
        async def fake_run(_db, _tenant_id, _run_id, job_id=None):
            return {"processed": 3, "upgraded": 3 - failures, "persist_failures": failures, "comparison_failures": 0}

        return fake_run

    monkeypatch.setattr(database, "worker_async_session", fake_session)
    monkeypatch.setattr(database, "set_tenant_context_session", no_context)

    monkeypatch.setattr(agent_task, "run_resolution_agent", summary_with(0))
    assert agent_task.recon_resolution_agent.run(str(uuid.uuid4()), str(uuid.uuid4()))["upgraded"] == 3

    monkeypatch.setattr(agent_task, "run_resolution_agent", summary_with(1))
    with pytest.raises(agent_task.ResolutionItemsNotPersistedError, match="1 of 3"):
        agent_task.recon_resolution_agent.run(str(uuid.uuid4()), str(uuid.uuid4()))
    assert disposed == [True, True]  # the per-task engine is disposed on the failing run too


async def test_a_failed_reload_recovers_the_session_for_the_items_after_it(db, tenant_a, monkeypatch):
    """A reload that fails with a database error leaves the transaction aborted; without
    recovery every later item would fail on it and run without tenant context."""
    from decimal import Decimal

    from sqlalchemy import text

    from app.core.database import set_tenant_context_session
    from app.services.reconciliation import resolution_agent
    from app.services.reconciliation.resolution_planner import plan_run
    from tests.conftest import create_test_recon_result, create_test_recon_run

    await enable_feature_flag(db, tenant_a.id, "reconciliation")
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_agent")
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    for i in range(3):
        await create_test_recon_result(
            db, tenant_a.id, run.id, status="pending", bucket="needs_review", match_type="deterministic",
            variance_type="manual_adjustment", variance_amount=Decimal("77.10"), stripe_amount=Decimal("500.00"),
            netsuite_amount=Decimal("422.90"), evidence={"charge_source_id": f"ch_{i}", "order_reference": f"R62848927{i}"},
        )  # fmt: skip
    await db.flush()
    await seed_run_linked_evidence(db, tenant_a.id, run.id)
    await plan_run(db, tenant_a.id, run.id)
    adapter = FakeAdapter(action="book_fee_line", narrative="Fee.")

    async def fake_config(_db, _tenant_id):
        return ("anthropic", "test-model", "sk-test", False)

    monkeypatch.setattr(agent_task, "get_adapter", lambda provider, api_key: adapter)
    monkeypatch.setattr(agent_task, "get_tenant_ai_config", fake_config)
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "off")

    tenant_id = str(tenant_a.id)
    await set_tenant_context_session(db, tenant_id)
    seen, real_apply, real_refresh = [], resolution_agent.apply_agent_proposal, db.refresh
    refreshes = []

    async def first_apply_fails(session, item, validated):
        seen.append((await session.execute(text("SELECT current_setting('app.current_tenant_id', true)"))).scalar())
        if len(seen) == 1:
            raise RuntimeError("connection reset")
        return await real_apply(session, item, validated)

    async def first_reload_breaks_the_transaction(item, *args, **kwargs):
        refreshes.append(item)
        if len(refreshes) == 1:
            await db.execute(text("SELECT 1/0"))  # a real database error: the transaction is now aborted
        return await real_refresh(item, *args, **kwargs)

    monkeypatch.setattr(resolution_agent, "apply_agent_proposal", first_apply_fails)
    monkeypatch.setattr(db, "refresh", first_reload_breaks_the_transaction)
    summary = await agent_task.run_resolution_agent(db, tenant_id, str(run.id))
    assert summary["processed"] == 3 and summary["persist_failures"] == 2
    assert seen == [tenant_id, tenant_id]  # the third item was applied, under the tenant's context


# ── per-tenant key and mode (typesafe.access), decided 2026-09-24 ──────────


async def _run_with_connection(db, tenant, monkeypatch, *, api_key=None, mode=None, platform_key="platform-test-key"):
    from tests.test_jev_access import _connect

    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", platform_key)
    if api_key or mode:
        await _connect(db, tenant, api_key=api_key, mode=mode)
    await enable_feature_flag(db, tenant.id, "reconciliation")
    await enable_feature_flag(db, tenant.id, "recon_resolution_agent")
    run, _ = await _seed_planned_run(db, tenant.id)
    adapter = FakeAdapter(action="book_fee_line", narrative="Book the difference as a fee line.")
    keys = []

    async def fake_config(_db, _tenant_id):
        return ("anthropic", "test-model", "sk-test", False)

    async def fake_try_ask(tenant_id, state=None, questions=None, *, build=None, api_key=None, **_):
        keys.append(api_key)
        return _jev_answer("carry_forward", 0.97), None

    monkeypatch.setattr(agent_task, "get_adapter", lambda provider, api_key: adapter)
    monkeypatch.setattr(agent_task, "get_tenant_ai_config", fake_config)
    monkeypatch.setattr(rj, "try_ask", fake_try_ask)
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "live")

    await agent_task.run_resolution_agent(db, str(tenant.id), str(run.id))
    events = (
        (
            await db.execute(
                select(AuditEvent).where(AuditEvent.tenant_id == tenant.id, AuditEvent.action == "recon.jev_comparison")
            )
        )
        .scalars()
        .all()
    )
    return keys, events


async def test_jev_is_on_by_default_with_the_platform_key(db, tenant_a, monkeypatch):
    keys, events = await _run_with_connection(db, tenant_a, monkeypatch)
    assert keys and set(keys) == {"platform-test-key"}
    assert {e.payload["mode"] for e in events} == {"live"}


async def test_the_tenants_own_key_is_the_one_sent(db, tenant_a, monkeypatch):
    keys, _ = await _run_with_connection(db, tenant_a, monkeypatch, api_key="tenant-own-key")
    assert keys and set(keys) == {"tenant-own-key"}


async def test_a_tenant_in_shadow_keeps_the_llm_deciding(db, tenant_a, monkeypatch):
    _, events = await _run_with_connection(db, tenant_a, monkeypatch, mode="shadow")
    assert events and {e.payload["mode"] for e in events} == {"shadow"}
    assert {e.payload["decided_by"] for e in events} <= {"llm", "guard"}


async def test_without_any_key_jev_is_never_called(db, tenant_a, monkeypatch):
    keys, events = await _run_with_connection(db, tenant_a, monkeypatch, platform_key="")
    assert keys == [] and events == []


# ── codex review of #314 ───────────────────────────────────────────────────


async def _seed_two_items(db, tenant):
    from decimal import Decimal

    from app.services.reconciliation.resolution_planner import plan_run
    from tests.conftest import create_test_recon_result, create_test_recon_run

    await enable_feature_flag(db, tenant.id, "reconciliation")
    await enable_feature_flag(db, tenant.id, "recon_resolution_agent")
    run = await create_test_recon_run(db, tenant.id, status="completed")
    for i in range(2):
        await create_test_recon_result(
            db, tenant.id, run.id, status="pending", bucket="needs_review", match_type="deterministic",
            variance_type="manual_adjustment", variance_amount=Decimal("77.10"), stripe_amount=Decimal("500.00"),
            netsuite_amount=Decimal("422.90"), evidence={"charge_source_id": f"ch_{i}", "order_reference": f"R62848927{i}"},
        )  # fmt: skip
    await db.flush()
    await seed_run_linked_evidence(db, tenant.id, run.id)
    await plan_run(db, tenant.id, run.id)
    return run


async def test_switching_jev_off_mid_run_stops_it_for_the_items_after(db, tenant_a, monkeypatch):
    from tests.test_jev_access import _connect

    run = await _seed_two_items(db, tenant_a)
    adapter = FakeAdapter(action="book_fee_line", narrative="Fee.")
    keys = []

    async def fake_config(_db, _tenant_id):
        return ("anthropic", "test-model", "sk-test", False)

    async def fake_try_ask(tenant_id, state=None, questions=None, *, build=None, api_key=None, **_):
        keys.append(api_key)
        if len(keys) == 1:
            await _connect(db, tenant_a, mode="off")  # the tenant switches Jev off during the run
        return _jev_answer("needs_human", 0.3), None

    monkeypatch.setattr(agent_task, "get_adapter", lambda provider, api_key: adapter)
    monkeypatch.setattr(agent_task, "get_tenant_ai_config", fake_config)
    monkeypatch.setattr(rj, "try_ask", fake_try_ask)
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "live")

    summary = await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run.id))

    assert summary["processed"] == 2
    assert keys == ["platform-test-key"]  # Jev asked about the first item only


async def test_a_failed_access_lookup_runs_the_items_without_jev(db, tenant_a, monkeypatch):
    """The lookup failing with a real database error aborts the transaction; recovery rolls
    back, which expires every loaded item. Both items must still be classified."""
    from sqlalchemy import text

    from app.services.typesafe import access as access_module

    run = await _seed_two_items(db, tenant_a)
    adapter = FakeAdapter(action="book_fee_line", narrative="Fee.")
    keys = []
    real_resolve = access_module.resolve_access
    state = {"failed": False}

    async def flaky_resolve(session, tenant_id):
        if not state["failed"]:
            state["failed"] = True
            await session.execute(text("SELECT 1/0"))  # a real error: the transaction is now aborted
        return await real_resolve(session, tenant_id)

    async def fake_config(_db, _tenant_id):
        return ("anthropic", "test-model", "sk-test", False)

    async def fake_try_ask(tenant_id, state=None, questions=None, *, build=None, api_key=None, **_):
        keys.append(api_key)
        return _jev_answer("needs_human", 0.3), None

    monkeypatch.setattr(access_module, "resolve_access", flaky_resolve)
    monkeypatch.setattr(agent_task, "get_adapter", lambda provider, api_key: adapter)
    monkeypatch.setattr(agent_task, "get_tenant_ai_config", fake_config)
    monkeypatch.setattr(rj, "try_ask", fake_try_ask)
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "live")

    summary = await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run.id))

    assert summary["processed"] == 2 and summary["persist_failures"] == 0
    assert len(adapter.calls) == 2  # both items classified (Jev unsure, so the model decides)
    assert keys == ["platform-test-key"]  # Jev off for the item whose lookup failed, on for the next
