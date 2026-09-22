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
    }  # fmt: skip


async def test_live_confident_jev_is_applied_without_an_llm_call(db, tenant_a, monkeypatch):
    adapter, rows, events = await _run(
        db, tenant_a, monkeypatch, mode="live", jev_action="carry_forward", jev_confidence=0.97
    )
    assert [r.action for r in rows] == ["carry_forward"]
    assert adapter.calls == []
    assert events[0].payload["decided_by"] == "jev"
    assert not any(ch.isdigit() for ch in rows[0].narrative)


async def test_off_records_nothing(db, tenant_a, monkeypatch):
    _, rows, events = await _run(db, tenant_a, monkeypatch, mode="off", jev_action="carry_forward", jev_confidence=0.97)
    assert [r.action for r in rows] == ["book_fee_line"]
    assert events == []
