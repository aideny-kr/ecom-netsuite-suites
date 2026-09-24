"""Regressions for the six reviewed disagreements, using synthetic identities."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.reconciliation import ReconResolutionProposal
from app.services.reconciliation import resolution_jev as rj
from app.services.reconciliation.resolution_agent import gather_context, validate_output
from app.services.reconciliation.resolution_planner import plan_run
from app.services.reconciliation.resolution_verifier import fee_explained
from tests.conftest import create_test_payout_line, create_test_recon_result, create_test_recon_run
from tests.resolution_evidence_helpers import seed_linked_evidence
from tests.test_resolution_jev import _context, _jev, _patch_jev

MATERIALITY = (Decimal("50"), Decimal("0.01"))


@pytest.mark.parametrize(
    "stripe,ns,fee,foreign",
    [
        ("35.41", "35.35", "0.35", "TWD"),
        ("33.75", "33.71", "0.34", "CAD"),
        ("60.12", "60.08", "0.60", "CAD"),
        ("56.35", "56.48", "0.56", "CAD"),
        ("72.10", "72.09", "0.72", "CAD"),
    ],
)
async def test_reviewed_foreign_currency_residuals_are_held_by_planner_and_both_models(
    db,
    tenant_a,
    monkeypatch,
    stripe,
    ns,
    fee,
    foreign,
):
    run = await create_test_recon_run(db, tenant_a.id)
    result = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        stripe_amount=Decimal(stripe),
        netsuite_amount=Decimal(ns),
        variance_amount=abs(Decimal(stripe) - Decimal(ns)),
        variance_type="amount_mismatch",
        bucket="needs_review",
        evidence={"order_reference": "R123456789"},
    )
    await seed_linked_evidence(db, run, result, fee=Decimal(fee), transaction_currency=foreign)
    await plan_run(db, tenant_a.id, run.id)
    proposal = (
        await db.execute(
            select(ReconResolutionProposal).where(
                ReconResolutionProposal.result_id == result.id,
            )
        )
    ).scalar_one()
    assert proposal.action == "needs_human"
    context = await gather_context(db, tenant_a.id, proposal)
    assert context["matched_posting"]["id"] == str(result.deposit_id)
    assert context["matched_posting"]["transaction_currency"] == foreign
    assert rj.derive_facts(context)["currency_consistent"] is False
    assert not fee_explained(stripe, ns, context["variance_amount"], fee)

    # An overconfident Jev and a fallback insisting on a write-off both fail.
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "live")
    _patch_jev(monkeypatch, result=_jev("book_fee_line", 0.99))
    calls = []

    async def classify(*args):
        calls.append(True)
        return {"action": "writeoff_je", "narrative": "Small difference.", "key_evidence": []}

    monkeypatch.setattr(rj.resolution_agent, "classify_item", classify)
    out, audit = await rj.decide_item(tenant_a.id, None, "test", context, MATERIALITY)
    assert out["action"] == "needs_human" and calls == [True]
    assert audit["jev_veto"] and audit["guard_veto"] == "unverified_currency_or_linkage"


@pytest.mark.parametrize(
    "mutation", [None, "partial", "wrong_currency", "other_tenant", "extra_charge", "missing_timestamp"]
)
async def test_washout_requires_cached_same_order_events(db, tenant_a, tenant_b, mutation):
    run = await create_test_recon_run(db, tenant_a.id)
    charge = await create_test_payout_line(
        db,
        tenant_a.id,
        amount=Decimal("100"),
        fee=Decimal("0"),
        description="Order R123456789",
    )
    timestamp = int(datetime(2026, 9, 13, tzinfo=timezone.utc).timestamp())
    charge.raw_data = {"created": timestamp}
    refund = await create_test_payout_line(
        db,
        tenant_b.id if mutation == "other_tenant" else tenant_a.id,
        amount=Decimal("-99" if mutation == "partial" else "-100"),
        fee=Decimal("0"),
        line_type="refund",
        currency="CAD" if mutation == "wrong_currency" else "USD",
        description="Order R123456789",
    )
    refund.raw_data = None if mutation == "missing_timestamp" else {"created": timestamp}
    if mutation == "extra_charge":
        await create_test_payout_line(db, tenant_a.id, description="Order R123456789")
    result = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        match_type="unmatched",
        variance_type="missing_in_netsuite",
        stripe_amount=Decimal("100"),
        netsuite_amount=None,
        variance_amount=Decimal("100"),
        evidence={
            "order_reference": "R123456789",
            "charge_payout_line_id": str(charge.id),
            "washout": True,
            "refund_date": "2026-09-13",
        },
        bucket="needs_review",
    )
    await plan_run(db, tenant_a.id, run.id)
    proposal = (
        await db.execute(
            select(ReconResolutionProposal).where(
                ReconResolutionProposal.result_id == result.id,
            )
        )
    ).scalar_one()
    assert proposal.action == ("carry_forward" if mutation is None else "needs_human")
    context = await gather_context(db, tenant_a.id, proposal)
    assert context["verified_washout"] is (mutation is None)
    decision = validate_output(
        {"action": "carry_forward", "narrative": "Charge and refund wash out."}, context, MATERIALITY
    )
    assert decision["action"] == ("carry_forward" if mutation is None else "needs_human")


@pytest.mark.parametrize(
    "field,value",
    [("transaction_currency", None), ("related_payout_id", "R987654321"), ("subsidiary_id", "2"), ("amount", "96.79")],
)
def test_missing_or_conflicting_linked_evidence_blocks_both_financial_actions(field, value):
    context = _context()
    context["matched_posting"][field] = value
    for action in ("book_fee_line", "writeoff_je"):
        assert validate_output({"action": action}, context, MATERIALITY)["action"] == "needs_human"


def test_unrelated_fuzzy_candidates_do_not_change_matched_basis():
    context = _context(candidate_postings=[{"amount": "100", "currency": "JPY", "memo": "unrelated"}])
    assert validate_output({"action": "book_fee_line"}, context, MATERIALITY)["action"] == "book_fee_line"


async def test_scoped_live_override_keeps_other_tenants_in_shadow(monkeypatch):
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "shadow")
    monkeypatch.setattr(settings, "JEV_RECON_LIVE_TENANTS", "customer-a")
    _patch_jev(monkeypatch, result=_jev("book_fee_line", 0.99))
    calls = []

    async def classify(*args):
        calls.append(True)
        return {"action": "needs_human"}

    monkeypatch.setattr(rj.resolution_agent, "classify_item", classify)
    decision, audit = await rj.decide_item("customer-a", None, "test", _context(), MATERIALITY)
    assert decision["action"] == "book_fee_line" and audit["mode"] == "live" and calls == []
    decision, audit = await rj.decide_item("customer-b", None, "test", _context(), MATERIALITY)
    assert decision["action"] == "needs_human" and audit["mode"] == "shadow" and calls == [True]
    monkeypatch.setattr(settings, "JEV_RECON_RESOLUTION_MODE", "off")
    _, audit = await rj.decide_item("customer-a", None, "test", _context(), MATERIALITY)
    assert audit is None  # global kill switch wins


@pytest.mark.parametrize("kind,evidence", [("fx_rounding", {"deposit_unapplied": True}), ("fees", {})])
def test_early_planner_rules_cannot_bypass_currency_proof(kind, evidence):
    from app.services.reconciliation.resolution_planner import plan_result

    out = plan_result(
        match_type="deterministic",
        variance_type=kind,
        variance_amount=Decimal("0.04"),
        stripe_amount=Decimal("35.41"),
        netsuite_amount=Decimal("35.37"),
        currency="USD",
        variance_explanation=None,
        evidence=evidence,
        already_posted=False,
        materiality_abs=Decimal("50"),
        materiality_pct=Decimal("0.01"),
        currency_basis_verified=False,
        fee_amount=Decimal("0.35"),
    )
    assert out.action == "needs_human"
