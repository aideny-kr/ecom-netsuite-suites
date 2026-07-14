"""ResolutionPlanner rule engine — exhaustive over the spec's ordered rules.

Spec: docs/superpowers/specs/2026-07-06-recon-summary-first-resolution-design.md
(mapping table, rules 1-10; first match wins; policy gates (chargeback) beat
evidence rules (deposit_unapplied), which beat variance-type dispatch).
"""

from decimal import Decimal

from app.services.reconciliation.resolution_planner import (
    VEHICLE_BY_ACTION,
    group_key_for,
    plan_result,
)

MAT = {"materiality_abs": Decimal("50"), "materiality_pct": Decimal("0.01")}


def _plan(**over):
    base = dict(
        match_type="deterministic",
        variance_type=None,
        variance_amount=Decimal("0"),
        stripe_amount=Decimal("100.00"),
        netsuite_amount=Decimal("100.00"),
        currency="USD",
        variance_explanation=None,
        evidence={"charge_source_id": "ch_1", "order_reference": "R123456789"},
        already_posted=False,
        **MAT,
    )
    base.update(over)
    return plan_result(**base)


def test_rule1_guard_prior_posted_skips():
    assert _plan(already_posted=True, variance_type="fees", variance_amount=Decimal("3.20")) is None


def test_rule2_clean_match_skips():
    assert _plan() is None  # deterministic + zero variance never reaches a proposal


def test_rule3_chargeback_policy_gate():
    p = _plan(variance_type="chargeback", variance_amount=Decimal("42.00"))
    assert p.action == "needs_human"
    assert p.booking_vehicle == "none"


def test_rule4_unapplied_deposit_evidence_wins_over_variance_dispatch():
    p = _plan(
        variance_type="fees",
        variance_amount=Decimal("3.20"),
        evidence={"charge_source_id": "ch_1", "order_reference": "R123456789", "deposit_unapplied": True},
    )
    assert p.action == "apply_deposit"
    assert p.booking_vehicle == "depositapplication"


def test_rule5_duplicate_voids():
    p = _plan(variance_type="duplicate", variance_amount=Decimal("100.00"))
    assert p.action == "void_duplicate"
    assert p.booking_vehicle == "customerdeposit"
    assert p.proposed_amount == Decimal("100.00")  # netsuite_amount


def test_rule6_fees_book_fee_line():
    p = _plan(variance_type="fees", variance_amount=Decimal("3.20"))
    assert p.action == "book_fee_line"
    assert p.booking_vehicle == "deposit"
    assert p.proposed_amount == Decimal("3.20")
    assert p.root_cause == "fees"
    assert p.group_key == "fees:book_fee_line:deposit"


def test_rule7_missing_with_order_ref_creates_deposit():
    p = _plan(match_type="unmatched", variance_type="missing", variance_amount=Decimal("100.00"), netsuite_amount=None)
    assert p.action == "create_and_apply_deposit"
    assert p.booking_vehicle == "customerdeposit"
    assert p.proposed_amount == Decimal("100.00")  # stripe_amount


def test_rule7b_missing_without_order_ref_needs_human():
    p = _plan(
        match_type="unmatched",
        variance_type="missing",
        variance_amount=Decimal("100.00"),
        netsuite_amount=None,
        evidence={"charge_source_id": "ch_1"},
    )
    assert p.action == "needs_human"


def test_rule8_fx_under_materiality_writeoff_flagged_je():
    p = _plan(variance_type="fx_rounding", variance_amount=Decimal("0.04"))
    assert p.action == "writeoff_je"
    assert p.booking_vehicle == "journalentry"
    assert p.above_materiality is False


def test_rule8b_fx_above_materiality_needs_human():
    # $60 > $50 abs threshold on a $10k order
    p = _plan(variance_type="fx_rounding", variance_amount=Decimal("60.00"), stripe_amount=Decimal("10000.00"))
    assert p.action == "needs_human"
    assert p.above_materiality is True


def test_rule9_timing_carries_forward():
    p = _plan(variance_type="timing", variance_amount=Decimal("0"))
    assert p.action == "carry_forward"
    assert p.booking_vehicle == "none"
    assert p.proposed_amount == Decimal("0")


def test_rule10_manual_adjustment_needs_human():
    p = _plan(variance_type="manual_adjustment", variance_amount=Decimal("77.10"))
    assert p.action == "needs_human"


def test_rule10b_unknown_variance_type_needs_human_not_crash():
    p = _plan(variance_type="future_type", variance_amount=Decimal("5.00"))
    assert p.action == "needs_human"  # total function: unknown → safe default


def test_above_materiality_set_on_every_proposal():
    p = _plan(variance_type="fees", variance_amount=Decimal("120.00"), stripe_amount=Decimal("10000.00"))
    assert p.action == "book_fee_line"  # materiality never changes action selection…
    assert p.above_materiality is True  # …only the bulk-approve eligibility flag


def test_narrative_embeds_explanation_and_no_invented_numbers():
    p = _plan(
        variance_type="fees",
        variance_amount=Decimal("3.20"),
        variance_explanation="Variance of $3.20 matches Stripe processing fee",
    )
    assert "Variance of $3.20 matches Stripe processing fee" in p.narrative


def test_group_key_derived_from_columns():
    assert group_key_for("fees", "book_fee_line", "deposit") == "fees:book_fee_line:deposit"
    assert VEHICLE_BY_ACTION["carry_forward"] == "none"


def test_chargeback_gate_preempts_unapplied_evidence():
    """Policy gates beat evidence rules: a chargeback with deposit_unapplied
    evidence must still go to needs_human, never apply_deposit."""
    p = _plan(
        variance_type="chargeback",
        variance_amount=Decimal("42.00"),
        evidence={"charge_source_id": "ch_1", "order_reference": "R1", "deposit_unapplied": True},
    )
    assert p.action == "needs_human"
    assert p.root_cause == "chargeback"


# ---------------------------------------------------------------------------
# Order-level taxonomy (2026-07-13): missing_in_netsuite, amount_mismatch,
# zero-variance fuzzy skip, recency guard, fee-explained decomposition.
# ---------------------------------------------------------------------------


def test_rule2b_zero_variance_fuzzy_match_skips():
    p = _plan(match_type="fuzzy", variance_type=None, variance_amount=Decimal("0"))
    assert p is None


def test_rule2b_zero_variance_fuzzy_match_empty_string_variance_type_skips():
    p = _plan(match_type="fuzzy", variance_type="", variance_amount=Decimal("0"))
    assert p is None


def test_rule2b_does_not_swallow_deposit_unapplied_evidence():
    """A fuzzy zero-variance match still carrying deposit_unapplied evidence
    must reach rule 4 (apply_deposit), not be dropped by the 2b skip — 2b is
    only for the no-evidence approve-the-match case."""
    p = _plan(
        match_type="fuzzy",
        variance_type=None,
        variance_amount=Decimal("0"),
        evidence={"charge_source_id": "ch_1", "order_reference": "R1", "deposit_unapplied": True},
    )
    assert p is not None
    assert p.action == "apply_deposit"


def test_rule7_missing_in_netsuite_with_order_ref_old_payout_creates_deposit():
    p = _plan(
        match_type="unmatched",
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("100.00"),
        netsuite_amount=None,
        days_since_payout=30,
    )
    assert p.action == "create_and_apply_deposit"
    assert p.root_cause == "missing_in_netsuite"


def test_rule7_missing_in_netsuite_recent_payout_carries_forward():
    p = _plan(
        match_type="unmatched",
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("100.00"),
        netsuite_amount=None,
        days_since_payout=7,
    )
    assert p.action == "carry_forward"
    assert p.booking_vehicle == "none"
    assert p.root_cause == "missing_in_netsuite"  # raw variance_type, not "timing"


def test_rule7_missing_in_netsuite_recent_payout_boundary_8_days_not_recent():
    p = _plan(
        match_type="unmatched",
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("100.00"),
        netsuite_amount=None,
        days_since_payout=8,
    )
    assert p.action == "create_and_apply_deposit"


def test_rule7_missing_in_netsuite_no_order_ref_needs_human():
    p = _plan(
        match_type="unmatched",
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("100.00"),
        netsuite_amount=None,
        evidence={"charge_source_id": "ch_1"},
        days_since_payout=30,
    )
    assert p.action == "needs_human"


def test_rule7b_amount_mismatch_fee_explained_books_fee_line():
    p = _plan(
        variance_type="amount_mismatch",
        variance_amount=Decimal("3.20"),
        fee_amount=Decimal("3.00"),
    )
    assert p.action == "book_fee_line"
    assert p.root_cause == "amount_mismatch"


def test_rule7b_amount_mismatch_fee_explained_ignores_materiality():
    # variance is above materiality on a small stripe_amount, but fee-match still wins
    p = _plan(
        variance_type="amount_mismatch",
        variance_amount=Decimal("60.20"),
        stripe_amount=Decimal("100.00"),
        fee_amount=Decimal("60.00"),
    )
    assert p.action == "book_fee_line"
    assert p.above_materiality is True


def test_rule7b_amount_mismatch_small_writes_off():
    p = _plan(
        variance_type="amount_mismatch",
        variance_amount=Decimal("0.04"),
        fee_amount=None,
    )
    assert p.action == "writeoff_je"
    assert p.root_cause == "amount_mismatch"
    assert p.above_materiality is False
    # narrative honesty: amount_mismatch is not FX/rounding — must not borrow
    # fx_rounding's wording (real fx_rounding rows are unaffected, see below).
    assert "Small residual amount mismatch" in p.narrative
    assert "FX/rounding" not in p.narrative


def test_rule7b_amount_mismatch_large_needs_human():
    p = _plan(
        variance_type="amount_mismatch",
        variance_amount=Decimal("60.00"),
        stripe_amount=Decimal("10000.00"),
        fee_amount=None,
    )
    assert p.action == "needs_human"
    assert p.root_cause == "amount_mismatch"
    assert p.above_materiality is True
    assert "Amount mismatch above materiality" in p.narrative
    assert "FX/rounding" not in p.narrative


def test_rule7b_amount_mismatch_no_fee_amount_delegates_to_fx_rounding_semantics():
    p = _plan(variance_type="amount_mismatch", variance_amount=Decimal("0.04"), fee_amount=None)
    assert p.action == "writeoff_je"


def test_fx_rounding_narratives_unchanged_by_amount_mismatch_wording():
    """Real fx_rounding rows must keep the FX/rounding narrative — only the
    amount_mismatch fallback path gets the new honest wording."""
    small = _plan(variance_type="fx_rounding", variance_amount=Decimal("0.04"))
    assert "FX/rounding difference" in small.narrative
    large = _plan(variance_type="fx_rounding", variance_amount=Decimal("60.00"), stripe_amount=Decimal("10000.00"))
    assert "FX/rounding variance above materiality" in large.narrative


def test_days_since_payout_and_fee_amount_default_none_behave_as_before():
    p = _plan(match_type="unmatched", variance_type="missing", variance_amount=Decimal("100.00"), netsuite_amount=None)
    assert p.action == "create_and_apply_deposit"


def test_rule4_amount_mismatch_does_not_swallow_unapplied_evidence():
    """T2 gate finding: rule 4 (deposit_unapplied evidence) must not preempt
    the amount_mismatch dispatch — the deposit's amount is KNOWN to be wrong,
    so returning apply_deposit here would auto-apply a wrong-amount deposit,
    bypassing the fee/materiality logic in rule 7b. An amount_mismatch row
    must resolve through the mismatch dispatch first."""
    p = _plan(
        variance_type="amount_mismatch",
        variance_amount=Decimal("3.20"),
        fee_amount=Decimal("3.00"),
        evidence={"charge_source_id": "ch_1", "order_reference": "R123456789", "deposit_unapplied": True},
    )
    assert p.action == "book_fee_line"
    assert p.action != "apply_deposit"


def test_variance_type_literal_includes_new_order_taxonomy_strings():
    from typing import get_args

    from app.schemas.reconciliation import VarianceType

    assert "missing_in_netsuite" in get_args(VarianceType)
    assert "amount_mismatch" in get_args(VarianceType)
