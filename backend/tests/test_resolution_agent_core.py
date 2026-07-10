"""ResolutionAgent core: gather_context, classify_item (one LLM call),
validate_output (allowlist + materiality + no-LLM-numbers contract), and
apply_agent_proposal (supersede-then-insert, scoped to one row).

Uses a FakeAdapter shaped like BaseLLMAdapter.create_message so no network
call is made; mirrors LLMResponse/ToolUseBlock from
backend/app/services/chat/llm_adapter.py.
"""

import uuid
from decimal import Decimal

from sqlalchemy import select

from app.models.canonical import NetsuitePosting
from app.models.reconciliation import ReconResolutionProposal
from app.services.chat.llm_adapter import LLMResponse, ToolUseBlock
from app.services.reconciliation.resolution_agent import (
    apply_agent_proposal,
    classify_item,
    fetch_agent_eligible,
    gather_context,
    validate_output,
)
from app.services.reconciliation.resolution_planner import plan_run
from tests.conftest import create_test_recon_result, create_test_recon_run

MATERIALITY = (Decimal("50"), Decimal("0.01"))


class FakeAdapter:
    """Canned single-tool-use response — mirrors the real adapter's return shape."""

    def __init__(self, action: str, narrative: str, key_evidence: list[str] | None = None):
        self.action = action
        self.narrative = narrative
        self.key_evidence = key_evidence or []
        self.calls: list[dict] = []

    async def create_message(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(
            tool_use_blocks=[
                ToolUseBlock(
                    id="tu_1",
                    name="classify_resolution",
                    input={
                        "action": self.action,
                        "narrative": self.narrative,
                        "key_evidence": self.key_evidence,
                    },
                )
            ]
        )


async def _seed_needs_human(db, tenant_id, *, variance_amount=Decimal("77.10"), variance_type="manual_adjustment"):
    """Seed one manual_adjustment result, plan it, return the resulting needs_human proposal."""
    run = await create_test_recon_run(db, tenant_id, status="completed")
    result = await create_test_recon_result(
        db,
        tenant_id,
        run.id,
        status="pending",
        bucket="needs_review",
        match_type="deterministic",
        variance_type=variance_type,
        variance_amount=variance_amount,
        stripe_amount=Decimal("500.00"),
        netsuite_amount=Decimal("422.90"),
        evidence={"charge_source_id": f"ch_{uuid.uuid4().hex[:8]}", "order_reference": "R628489275"},
    )
    await db.flush()
    await plan_run(db, tenant_id, run.id)
    eligible = await fetch_agent_eligible(db, tenant_id, run.id)
    assert len(eligible) == 1
    return run, result, eligible[0]


async def test_happy_path_upgrades_needs_human_to_agent_action(db, tenant_a):
    _run, _result, proposal = await _seed_needs_human(db, tenant_a.id)
    assert proposal.root_cause == "manual_adjustment"
    assert proposal.action == "needs_human"
    assert proposal.source == "planner"

    context = await gather_context(db, tenant_a.id, proposal)
    assert context["variance_amount"] == "77.10"

    adapter = FakeAdapter(
        action="book_fee_line",
        narrative="Unexplained variance of $77.10 against a $500.00 charge — book as a fee line.",
        key_evidence=["variance_amount=77.10"],
    )
    out = await classify_item(adapter, "test-model", context)
    assert out["action"] == "book_fee_line"
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["tool_choice"] == {"type": "tool", "name": "classify_resolution"}

    validated = validate_output(out, context, MATERIALITY)
    assert validated["action"] == "book_fee_line"
    assert "contract_violation" not in validated

    applied = await apply_agent_proposal(db, proposal, validated)
    assert applied is True

    old = (
        await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.id == proposal.id))
    ).scalar_one()
    assert old.status == "superseded"

    new = (
        (
            await db.execute(
                select(ReconResolutionProposal).where(
                    ReconResolutionProposal.result_id == proposal.result_id,
                    ReconResolutionProposal.source == "agent",
                )
            )
        )
        .scalars()
        .one()
    )
    assert new.status == "proposed"
    assert new.action == "book_fee_line"
    assert new.group_key == "manual_adjustment:book_fee_line:deposit"
    assert new.narrative == adapter.narrative
    assert new.evidence["agent_key_evidence"] == ["variance_amount=77.10"]
    assert new.proposed_amount == proposal.proposed_amount
    assert new.currency == proposal.currency
    assert new.charge_source_id == proposal.charge_source_id


async def test_invented_number_degrades_to_needs_human(db, tenant_a):
    _run, _result, proposal = await _seed_needs_human(db, tenant_a.id)
    context = await gather_context(db, tenant_a.id, proposal)

    adapter = FakeAdapter(action="book_fee_line", narrative="Roughly $999.99 of unexplained variance.")
    out = await classify_item(adapter, "test-model", context)
    validated = validate_output(out, context, MATERIALITY)

    assert validated["action"] == "needs_human"
    assert validated["contract_violation"]


async def test_disallowed_action_degrades_to_needs_human(db, tenant_a):
    _run, _result, proposal = await _seed_needs_human(db, tenant_a.id)
    context = await gather_context(db, tenant_a.id, proposal)

    adapter = FakeAdapter(action="credit_memo_refund", narrative="Refund the customer.")
    out = await classify_item(adapter, "test-model", context)
    validated = validate_output(out, context, MATERIALITY)

    assert validated["action"] == "needs_human"
    assert "contract_violation" in validated


async def test_writeoff_je_above_materiality_degrades(db, tenant_a):
    # 100.00 variance > 50 abs materiality threshold → above_materiality
    _run, _result, proposal = await _seed_needs_human(db, tenant_a.id, variance_amount=Decimal("100.00"))
    assert proposal.above_materiality is True
    context = await gather_context(db, tenant_a.id, proposal)

    adapter = FakeAdapter(
        action="writeoff_je",
        narrative="Write off the $100.00 variance.",
        key_evidence=["variance_amount=100.00"],
    )
    out = await classify_item(adapter, "test-model", context)
    validated = validate_output(out, context, MATERIALITY)

    assert validated["action"] == "needs_human"
    assert "contract_violation" in validated


async def test_apply_agent_proposal_noops_when_no_longer_eligible(db, tenant_a):
    _run, _result, proposal = await _seed_needs_human(db, tenant_a.id)
    # human decided meanwhile
    proposal.status = "approved"
    await db.flush()

    out = {"action": "book_fee_line", "narrative": "n/a", "key_evidence": []}
    applied = await apply_agent_proposal(db, proposal, out)
    assert applied is False

    agent_rows = (
        (
            await db.execute(
                select(ReconResolutionProposal).where(
                    ReconResolutionProposal.result_id == proposal.result_id,
                    ReconResolutionProposal.source == "agent",
                )
            )
        )
        .scalars()
        .all()
    )
    assert agent_rows == []


async def test_apply_agent_proposal_noops_when_result_went_terminal(db, tenant_a):
    """A result can go terminal (e.g. locked via the classic per-result approve
    path) independently of its proposal row, which is still 'proposed' — the
    agent must not supersede/insert for a result it no longer has any
    business touching, mirroring approve_group_core's not_terminal_result
    guard."""
    from app.models.reconciliation import ReconciliationResult

    _run, result, proposal = await _seed_needs_human(db, tenant_a.id)
    result.status = "locked"
    await db.flush()

    out = {"action": "book_fee_line", "narrative": "n/a", "key_evidence": []}
    applied = await apply_agent_proposal(db, proposal, out)
    assert applied is False

    refreshed = (
        await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.id == proposal.id))
    ).scalar_one()
    assert refreshed.status == "proposed"  # planner row untouched, not superseded

    agent_rows = (
        (
            await db.execute(
                select(ReconResolutionProposal).where(
                    ReconResolutionProposal.result_id == proposal.result_id,
                    ReconResolutionProposal.source == "agent",
                )
            )
        )
        .scalars()
        .all()
    )
    assert agent_rows == []

    locked_result = (
        await db.execute(select(ReconciliationResult).where(ReconciliationResult.id == result.id))
    ).scalar_one()
    assert locked_result.status == "locked"


async def test_gather_context_tenant_scoped_candidate_postings(db, tenant_a, tenant_b):
    _run, _result, proposal = await _seed_needs_human(db, tenant_a.id)

    # tenant_a posting within ±5% of stripe_amount (500.00) — should surface
    db.add(
        NetsuitePosting(
            tenant_id=tenant_a.id,
            dedupe_key=f"dk-a-{uuid.uuid4().hex}",
            source="netsuite",
            source_id="ns-a-1",
            record_type="customerdeposit",
            amount=Decimal("495.00"),
            currency="USD",
            memo="Order R628489275 deposit",
        )
    )
    # tenant_b posting, same amount/memo — must NOT surface
    db.add(
        NetsuitePosting(
            tenant_id=tenant_b.id,
            dedupe_key=f"dk-b-{uuid.uuid4().hex}",
            source="netsuite",
            source_id="ns-b-1",
            record_type="customerdeposit",
            amount=Decimal("495.00"),
            currency="USD",
            memo="Order R628489275 deposit",
        )
    )
    await db.flush()

    context = await gather_context(db, tenant_a.id, proposal)
    postings = context["candidate_postings"]
    assert len(postings) == 1
    assert postings[0]["netsuite_internal_id"] == "" or postings[0]["record_type"] == "customerdeposit"
    assert all(isinstance(v, str) for p in postings for v in p.values())
    assert postings[0]["amount"] == "495.00"


async def test_gather_context_amount_window_handles_negative_stripe_amount(db, tenant_a):
    """A negative stripe_amount (refund/chargeback) must not silently invert
    the BETWEEN bounds — lower/upper picked by raw arithmetic rather than
    min/max would build a window like between(-105, -95) with the wrong sign,
    which always matches zero rows."""
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    result = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        bucket="needs_review",
        match_type="deterministic",
        variance_type="manual_adjustment",
        variance_amount=Decimal("2.00"),
        stripe_amount=Decimal("-100.00"),
        netsuite_amount=Decimal("-102.00"),
        evidence={"charge_source_id": f"ch_{uuid.uuid4().hex[:8]}", "order_reference": "R9"},
    )
    await db.flush()
    await plan_run(db, tenant_a.id, run.id)
    eligible = await fetch_agent_eligible(db, tenant_a.id, run.id)
    assert len(eligible) == 1
    proposal = eligible[0]
    assert proposal.result_id == result.id

    db.add(
        NetsuitePosting(
            tenant_id=tenant_a.id,
            dedupe_key=f"dk-neg-{uuid.uuid4().hex}",
            source="netsuite",
            source_id="ns-neg-1",
            record_type="customerdeposit",
            amount=Decimal("-98.00"),
            currency="USD",
            memo="Refund adjustment",
        )
    )
    await db.flush()

    context = await gather_context(db, tenant_a.id, proposal)
    postings = context["candidate_postings"]
    assert len(postings) == 1
    assert postings[0]["amount"] == "-98.00"


async def test_validate_output_pins_chargeback_to_needs_human_regardless_of_action(db, tenant_a):
    """Chargeback policy pin: even when the model classifies a chargeback item
    with an otherwise-allowed action, the code-side validator overrides it to
    needs_human — the policy must not depend on the model choosing correctly."""
    _run, _result, proposal = await _seed_needs_human(db, tenant_a.id, variance_type="chargeback")
    context = await gather_context(db, tenant_a.id, proposal)
    assert context["root_cause"] == "chargeback"

    adapter = FakeAdapter(
        action="book_fee_line",
        narrative="Book the variance as a fee line.",
        key_evidence=["variance_amount=77.10"],
    )
    out = await classify_item(adapter, "test-model", context)
    validated = validate_output(out, context, MATERIALITY)

    assert validated["action"] == "needs_human"
    assert validated["contract_violation"] == "chargeback_policy"
