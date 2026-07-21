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


def test_rule2b_empty_string_variance_type_does_not_skip():
    """Gate r2: four_bucket_classifier._has_variance treats an empty-string
    variance_type as HAVING variance (only None means no variance signal), so
    the 2b skip must not swallow it — it must fall through to a real
    disposition (needs_human, the rule-10 tail) instead of vanishing."""
    p = _plan(match_type="fuzzy", variance_type="", variance_amount=Decimal("0"))
    assert p is not None
    assert p.action == "needs_human"


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


def test_rule7_failed_payout_recent_charge_needs_human_not_carry_forward():
    """Gate r2 Fix C: a recent charge tied to a FAILED payout must not be
    treated as sync-lag — the payout never settled, so carry_forward would be
    wrong regardless of how recent the (non-)arrival looks."""
    p = _plan(
        match_type="unmatched",
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("100.00"),
        netsuite_amount=None,
        days_since_payout=1,
        payout_status="failed",
    )
    assert p.action == "needs_human"
    assert "payout failed" in p.narrative.lower()


def test_rule7_canceled_payout_recent_charge_needs_human():
    p = _plan(
        match_type="unmatched",
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("100.00"),
        netsuite_amount=None,
        days_since_payout=1,
        payout_status="canceled",
    )
    assert p.action == "needs_human"


def test_rule7_healthy_payout_recent_charge_carries_forward_unchanged():
    for status in ("paid", "pending", "in_transit"):
        p = _plan(
            match_type="unmatched",
            variance_type="missing_in_netsuite",
            variance_amount=Decimal("100.00"),
            netsuite_amount=None,
            days_since_payout=1,
            payout_status=status,
        )
        assert p.action == "carry_forward", status


def test_rule7_unknown_payout_status_recent_charge_behaves_as_before():
    """payout_status=None (no payout row joined — enrichment couldn't
    determine health) must not be treated as proof the payout died; behaves
    exactly as pre-Fix-C (recency alone decides)."""
    p = _plan(
        match_type="unmatched",
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("100.00"),
        netsuite_amount=None,
        days_since_payout=1,
        payout_status=None,
    )
    assert p.action == "carry_forward"


def test_rule7_pending_payout_past_recency_window_needs_human():
    """Final wave Fix 1: past the recency window, a payout still pending or
    in_transit is unsettled — Stripe hasn't confirmed the funds landed, so
    proposing a NetSuite deposit would book against money that may never
    arrive. Must route to needs_human, not create_and_apply_deposit."""
    p = _plan(
        match_type="unmatched",
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("100.00"),
        netsuite_amount=None,
        days_since_payout=8,
        payout_status="pending",
    )
    assert p.action == "needs_human"
    assert "unsettled" in p.narrative.lower()


def test_rule7_paid_payout_past_recency_window_still_creates_deposit():
    """Unchanged: a settled (paid) payout past the recency window still
    proposes create_and_apply_deposit — only pending/in_transit is unsettled."""
    p = _plan(
        match_type="unmatched",
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("100.00"),
        netsuite_amount=None,
        days_since_payout=8,
        payout_status="paid",
    )
    assert p.action == "create_and_apply_deposit"


def test_rule7_pending_payout_within_recency_window_still_carries_forward():
    """Unchanged: inside the recency window, pending/in_transit is still
    plausibly sync-lag — the existing healthy-status recency branch owns it."""
    p = _plan(
        match_type="unmatched",
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("100.00"),
        netsuite_amount=None,
        days_since_payout=3,
        payout_status="pending",
    )
    assert p.action == "carry_forward"


def test_rule7b_amount_mismatch_fee_explained_books_fee_line():
    p = _plan(
        variance_type="amount_mismatch",
        variance_amount=Decimal("3.20"),
        netsuite_amount=Decimal("96.80"),  # < stripe_amount (100.00) — fee lowered NetSuite
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
        netsuite_amount=Decimal("39.80"),  # < stripe_amount — fee lowered NetSuite
        fee_amount=Decimal("60.00"),
    )
    assert p.action == "book_fee_line"
    assert p.above_materiality is True


def test_rule7b_amount_mismatch_fee_proximate_but_wrong_direction_not_fee_explained():
    """Gate r2 Fix B: a Stripe fee can only ever make NetSuite LOWER than
    Stripe. netsuite_amount HIGHER than stripe_amount by an amount close to
    fee_amount must NOT be misexplained as a fee — it must fall through to
    the materiality split instead."""
    p = _plan(
        variance_type="amount_mismatch",
        variance_amount=Decimal("3.00"),
        stripe_amount=Decimal("10000.00"),  # large base so 3.00 stays sub-materiality by % too
        netsuite_amount=Decimal("10003.00"),  # HIGHER than stripe — not fee-explainable
        fee_amount=Decimal("3.00"),
    )
    assert p.action != "book_fee_line"
    assert p.action == "writeoff_je"  # sub-materiality residual (abs 3.00 < 50, pct 0.03% < 1%)


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
        netsuite_amount=Decimal("96.80"),  # < stripe_amount (100.00) — fee lowered NetSuite
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


# ---------------------------------------------------------------------------
# Washout classification (Phase B Task 2, operator decision 2026-07-21):
# evidence.washout (attached by order_recon_job._washout_evidence, Task 1)
# routes to a permanent carry_forward instead of rule 7's
# create_and_apply_deposit — a charge refunded in full within 7 days never
# reaches NetSuite, so there is nothing to book.
# ---------------------------------------------------------------------------

WASHOUT_EVIDENCE = {
    "charge_source_id": "ch_washout",
    "order_reference": "R628489275",
    "washout": True,
    "refund_date": "2026-03-18",
    "refund_amount": "-100.00",
    "net_after_refund": "0.00",
}
WASHOUT_NARRATIVE = (
    "Stripe charge fully refunded on 2026-03-18 within 7 days; order canceled — no NetSuite booking required."
)


def test_washout_evidence_carries_forward_not_create_and_apply():
    p = _plan(
        match_type="unmatched",
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("100.00"),
        netsuite_amount=None,
        evidence=WASHOUT_EVIDENCE,
    )
    assert p.action == "carry_forward"
    assert p.action != "create_and_apply_deposit"
    assert p.booking_vehicle == "none"
    assert p.root_cause == "washout"
    assert p.group_key == "washout:carry_forward:none"


def test_washout_narrative_is_exact_evidence_sourced_template():
    p = _plan(
        match_type="unmatched",
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("100.00"),
        netsuite_amount=None,
        evidence=WASHOUT_EVIDENCE,
    )
    assert p.narrative == WASHOUT_NARRATIVE


def test_washout_wins_over_rule7_dispatch_even_with_old_payout():
    """Without washout evidence this exact shape (old payout, order ref known)
    would hit rule 7's create_and_apply_deposit — washout evidence must
    preempt it."""
    p = _plan(
        match_type="unmatched",
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("100.00"),
        netsuite_amount=None,
        days_since_payout=30,
        evidence=WASHOUT_EVIDENCE,
    )
    assert p.action == "carry_forward"
    assert p.root_cause == "washout"


def test_no_washout_evidence_rule7_unchanged():
    """Sanity: identical shape minus washout evidence still goes through the
    ordinary rule-7 dispatch (create_and_apply_deposit — old payout, order
    ref known)."""
    p = _plan(
        match_type="unmatched",
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("100.00"),
        netsuite_amount=None,
        days_since_payout=30,
        evidence={"charge_source_id": "ch_1", "order_reference": "R1"},
    )
    assert p.action == "create_and_apply_deposit"


def test_washout_false_evidence_does_not_trigger_washout_rule():
    """evidence['washout'] explicitly False (not just absent) must not
    trigger the washout rule — mirrors rule 4's `is True` evidence check."""
    p = _plan(
        match_type="unmatched",
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("100.00"),
        netsuite_amount=None,
        days_since_payout=30,
        evidence={"charge_source_id": "ch_1", "order_reference": "R1", "washout": False},
    )
    assert p.action == "create_and_apply_deposit"


def test_chargeback_gate_preempts_washout_evidence():
    """Precedence choice (documented in resolution_planner.py at the washout
    rule): a chargeback is the stricter policy pin and must win over washout
    evidence — funds already disputed/reversed via a chargeback are never
    silently downgraded to a no-booking washout carry_forward. The chargeback
    gate (rule 3) is checked before the washout rule, so this is automatic —
    this test locks the choice in."""
    p = _plan(
        variance_type="chargeback",
        variance_amount=Decimal("100.00"),
        evidence=WASHOUT_EVIDENCE,
    )
    assert p.action == "needs_human"
    assert p.root_cause == "chargeback"


def test_washout_not_in_recency_hold_root_causes():
    """A washout is permanent (order canceled), not a 're-check next run'
    sync-lag snooze — it must never get the recency-hold cross-run lifecycle
    (see RECENCY_HOLD_ROOT_CAUSES' design note in resolution_planner.py)."""
    from app.services.reconciliation.resolution_planner import RECENCY_HOLD_ROOT_CAUSES

    assert "washout" not in RECENCY_HOLD_ROOT_CAUSES


def test_washout_added_to_variance_type_literal():
    from typing import get_args

    from app.schemas.reconciliation import VarianceType

    assert "washout" in get_args(VarianceType)
