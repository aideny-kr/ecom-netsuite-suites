"""Phase 2 end-to-end: agent tail over a seeded run, all the way through
summary + group-approve.

Seeds one manual_adjustment (planner abstains → needs_human), one chargeback
(policy gate → needs_human), one fees result (planner resolves directly, no
agent involvement) → plans the run → drives the agent task fn directly with a
FakeAdapter (mirrors LLMResponse/ToolUseBlock, no network call) that:
  - upgrades the manual_adjustment item to book_fee_line with a contract-clean
    narrative (numbers taken verbatim from context)
  - returns needs_human (with an enriched narrative) for the chargeback item —
    it IS agent-eligible (planner/needs_human/proposed), but the allowlist and
    the fake's own classification keep it at needs_human; the planner row is
    still superseded and replaced by an agent-sourced needs_human row.
Then asserts the resolution-summary payload reflects the new agent group, and
that approve_group_core flips the manual_adjustment group's result to
'approved' — the same core the REST endpoint and chat tool share.
"""

from decimal import Decimal

from sqlalchemy import select

from app.api.v1.reconciliation import get_resolution_summary
from app.models.reconciliation import ReconResolutionProposal
from app.services.chat.llm_adapter import LLMResponse, ToolUseBlock
from app.services.reconciliation.group_actions import approve_group_core
from app.services.reconciliation.resolution_planner import plan_run
from app.workers.tasks import recon_resolution_agent as agent_task
from tests.conftest import (
    create_test_recon_result,
    create_test_recon_run,
    create_test_user,
    enable_feature_flag,
)


class FakeAdapter:
    """Classifies by root_cause found in the context — mirrors the real
    adapter's create_message return shape (LLMResponse/ToolUseBlock)."""

    def __init__(self):
        self.calls: list[dict] = []

    async def create_message(self, **kwargs):
        import json

        self.calls.append(kwargs)
        context = json.loads(kwargs["messages"][0]["content"])
        root_cause = context["root_cause"]
        if root_cause == "manual_adjustment":
            out = {
                "action": "book_fee_line",
                "narrative": (
                    f"Unexplained variance of ${context['variance_amount']} against a "
                    f"${context['stripe_amount']} charge — book as a fee line."
                ),
                "key_evidence": [f"variance_amount={context['variance_amount']}"],
            }
        else:
            out = {
                "action": "needs_human",
                "narrative": (
                    f"Chargeback of ${context['variance_amount']} — no candidate NetSuite "
                    "postings found; still needs human review."
                ),
                "key_evidence": [f"variance_amount={context['variance_amount']}"],
            }
        return LLMResponse(tool_use_blocks=[ToolUseBlock(id="tu_1", name="classify_resolution", input=out)])


async def _seed_run(db, tenant):
    user, _ = await create_test_user(db, tenant)
    await enable_feature_flag(db, tenant.id, "recon_resolution_ui")
    await enable_feature_flag(db, tenant.id, "reconciliation")
    await enable_feature_flag(db, tenant.id, "recon_resolution_agent")
    run = await create_test_recon_run(db, tenant.id, status="completed")

    manual_result = await create_test_recon_result(
        db,
        tenant.id,
        run.id,
        status="pending",
        bucket="needs_review",
        match_type="deterministic",
        variance_type="manual_adjustment",
        variance_amount=Decimal("9.00"),
        stripe_amount=Decimal("1000.00"),
        netsuite_amount=Decimal("991.00"),
        evidence={"charge_source_id": "ch_e2e_ma", "order_reference": "R628489275"},
    )
    chargeback_result = await create_test_recon_result(
        db,
        tenant.id,
        run.id,
        status="pending",
        bucket="needs_review",
        match_type="deterministic",
        variance_type="chargeback",
        variance_amount=Decimal("42.00"),
        stripe_amount=Decimal("42.00"),
        netsuite_amount=Decimal("0"),
        evidence={"charge_source_id": "ch_e2e_cb", "order_reference": "R2"},
    )
    await create_test_recon_result(
        db,
        tenant.id,
        run.id,
        status="pending",
        bucket="auto_classifications",
        match_type="deterministic",
        variance_type="fees",
        variance_amount=Decimal("9.00"),
        stripe_amount=Decimal("1000.00"),
        netsuite_amount=Decimal("991.00"),
        evidence={"charge_source_id": "ch_e2e_fee", "order_reference": "R1"},
    )
    run.matches_count = 0
    await db.flush()
    await plan_run(db, tenant.id, run.id)
    return user, run, manual_result, chargeback_result


async def test_agent_tail_e2e_through_summary_and_approve(db, tenant_a, monkeypatch):
    user, run, manual_result, chargeback_result = await _seed_run(db, tenant_a)

    fake_adapter = FakeAdapter()

    async def fake_get_tenant_ai_config(_db, _tenant_id):
        return ("anthropic", "test-model", "sk-test", False)

    monkeypatch.setattr(agent_task, "get_adapter", lambda provider, api_key: fake_adapter)
    monkeypatch.setattr(agent_task, "get_tenant_ai_config", fake_get_tenant_ai_config)

    summary = await agent_task.run_resolution_agent(db, str(tenant_a.id), str(run.id))
    assert summary == {
        "processed": 2,
        "upgraded": 1,
        "kept_needs_human": 1,
        "contract_violations": 0,
    }
    assert len(fake_adapter.calls) == 2

    props = (
        (await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run.id)))
        .scalars()
        .all()
    )
    manual_planner = [p for p in props if p.result_id == manual_result.id and p.source == "planner"]
    manual_agent = [p for p in props if p.result_id == manual_result.id and p.source == "agent"]
    assert len(manual_planner) == 1
    assert manual_planner[0].status == "superseded"
    assert len(manual_agent) == 1
    assert manual_agent[0].status == "proposed"
    assert manual_agent[0].action == "book_fee_line"
    assert manual_agent[0].group_key == "manual_adjustment:book_fee_line:deposit"

    cb_planner = [p for p in props if p.result_id == chargeback_result.id and p.source == "planner"]
    cb_agent = [p for p in props if p.result_id == chargeback_result.id and p.source == "agent"]
    assert len(cb_planner) == 1
    assert cb_planner[0].status == "superseded"
    assert len(cb_agent) == 1
    assert cb_agent[0].status == "proposed"
    assert cb_agent[0].action == "needs_human"
    assert "still needs human review" in cb_agent[0].narrative

    # Summary reflects the new agent-sourced group.
    out = await get_resolution_summary(str(run.id), user=user, db=db)
    keys = {g.group_key for g in out.groups}
    assert "manual_adjustment:book_fee_line:deposit" in keys
    assert "chargeback:needs_human:none" in keys
    manual_group = next(g for g in out.groups if g.group_key == "manual_adjustment:book_fee_line:deposit")
    assert manual_group.count == 1
    assert manual_group.proposed_count == 1

    # Approve the agent-upgraded group via the shared core (same path as the
    # REST endpoint and the chat tool) — the result flips.
    result = await approve_group_core(
        db,
        tenant_id=tenant_a.id,
        actor_id=user.id,
        run_id=str(run.id),
        group_key="manual_adjustment:book_fee_line:deposit",
        notes=None,
        included_above_materiality_ids=[],
        excluded_ids=[],
        currency=None,
    )
    assert result.approved_count == 1

    await db.refresh(manual_result)
    assert manual_result.status == "approved"

    # Chargeback result is untouched — needs_human groups are never
    # group-approvable and this test never tried to approve it.
    await db.refresh(chargeback_result)
    assert chargeback_result.status == "pending"
