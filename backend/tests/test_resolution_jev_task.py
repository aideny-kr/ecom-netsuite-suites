"""Worker-level proof for A1: the comparison is recorded, and who decided is honoured."""

from sqlalchemy import select

from app.core.config import settings
from app.models.audit import AuditEvent
from app.models.reconciliation import ReconResolutionProposal
from app.services.reconciliation import resolution_jev as rj
from app.services.reconciliation.resolution_agent import AGENT_ALLOWED_ACTIONS
from app.services.typesafe.client import JevResult
from app.workers.tasks import recon_resolution_agent as agent_task
from tests.conftest import enable_feature_flag
from tests.test_resolution_agent_task import FakeAdapter, _seed_planned_run


def _jev_answer(action, confidence):
    probabilities = {a: 0.0 for a in AGENT_ALLOWED_ACTIONS}
    probabilities[action] = 1.0
    answers = {"action": {"type": "choice", "choice": action, "probabilities": probabilities, "confidence": confidence}}
    return JevResult(answers=answers, model="jev-1.13.0", input_tokens=300, elapsed_ms=80)


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


async def test_live_confident_but_ineligible_jev_pick_abstains_without_an_llm_call(db, tenant_a, monkeypatch):
    """The seeded item is a manual_adjustment with no fee, timing, washout or recency fact,
    so carry_forward has no factual basis; the gate abstains and records why."""
    adapter, rows, events = await _run(
        db, tenant_a, monkeypatch, mode="live", jev_action="carry_forward", jev_confidence=0.97
    )
    assert [r.action for r in rows] == ["needs_human"]
    assert adapter.calls == []
    payload = events[0].payload
    assert payload["decided_by"] == "guard" and payload["eligibility_veto"] == "carry_forward"
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
