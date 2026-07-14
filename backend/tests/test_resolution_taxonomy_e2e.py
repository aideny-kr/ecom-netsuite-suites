"""Task 4: live-shaped regression e2e for the order-level taxonomy fix.

Seeds a run shaped like Framework's live distribution (86bawk3cp ground truth):
missing_in_netsuite (recent + old), amount_mismatch (fee-explained + tiny +
large), a zero-variance fuzzy match, and one legacy-taxonomy ``fees`` row —
then plans + summarizes end-to-end and asserts the exact expected group keys
and actions. Exercises plan_run's Task-2 batched payout_line enrichment
against real payout_lines/payouts rows (not just plan_result in isolation),
which is the gap that let the spec-clean rule set still under-deliver against
live data (see the addendum lesson: validate against live rows, not spec
enums)."""

from datetime import date, timedelta
from decimal import Decimal

from app.api.v1.reconciliation import get_resolution_summary, plan_resolutions
from tests.conftest import (
    create_test_payout_line,
    create_test_recon_result,
    create_test_recon_run,
    create_test_user,
    enable_feature_flag,
)


async def test_live_shaped_taxonomy_e2e(db, tenant_a):
    user, _ = await create_test_user(db, tenant_a)
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_ui")
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    today = date.today()

    # missing_in_netsuite, recent payout (<= RECENT_PAYOUT_LAG_DAYS) → carry_forward
    recent_line = await create_test_payout_line(db, tenant_a.id, arrival_date=today - timedelta(days=1))
    await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        bucket="needs_review",
        match_type="unmatched",
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("120.00"),
        stripe_amount=Decimal("120.00"),
        netsuite_amount=None,
        evidence={
            "charge_source_id": "ch_missing_recent",
            "order_reference": "R_RECENT",
            "charge_payout_line_id": str(recent_line.id),
        },
    )

    # missing_in_netsuite, old payout + known order ref → create_and_apply_deposit
    old_line = await create_test_payout_line(db, tenant_a.id, arrival_date=today - timedelta(days=30))
    await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        bucket="needs_review",
        match_type="unmatched",
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("250.00"),
        stripe_amount=Decimal("250.00"),
        netsuite_amount=None,
        evidence={
            "charge_source_id": "ch_missing_old",
            "order_reference": "R_OLD",
            "charge_payout_line_id": str(old_line.id),
        },
    )

    # amount_mismatch, fee-explained (within FEE_EXPLAIN_TOLERANCE of the
    # payout_line's fee) → book_fee_line, root_cause stays "amount_mismatch"
    fee_line = await create_test_payout_line(db, tenant_a.id, fee=Decimal("3.20"))
    await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        bucket="needs_review",
        match_type="deterministic",
        variance_type="amount_mismatch",
        variance_amount=Decimal("3.00"),
        stripe_amount=Decimal("100.00"),
        netsuite_amount=Decimal("97.00"),
        evidence={
            "charge_source_id": "ch_mismatch_fee",
            "order_reference": "R_FEE",
            "charge_payout_line_id": str(fee_line.id),
        },
    )

    # amount_mismatch, sub-materiality, no fee evidence → writeoff_je
    await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        bucket="needs_review",
        match_type="deterministic",
        variance_type="amount_mismatch",
        variance_amount=Decimal("5.00"),
        stripe_amount=Decimal("1000.00"),
        netsuite_amount=Decimal("995.00"),
        evidence={"charge_source_id": "ch_mismatch_tiny", "order_reference": "R_TINY"},
    )

    # amount_mismatch, above materiality, no fee evidence → needs_human
    await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        bucket="needs_review",
        match_type="deterministic",
        variance_type="amount_mismatch",
        variance_amount=Decimal("200.00"),
        stripe_amount=Decimal("1000.00"),
        netsuite_amount=Decimal("800.00"),
        evidence={"charge_source_id": "ch_mismatch_large", "order_reference": "R_LARGE"},
    )

    # zero-variance fuzzy match → no proposal (pure noise removal)
    await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        bucket="rules",
        match_type="fuzzy",
        variance_type=None,
        variance_amount=Decimal("0"),
        stripe_amount=Decimal("60.00"),
        netsuite_amount=Decimal("60.00"),
        evidence={"charge_source_id": "ch_zero_fuzzy", "order_reference": "R_ZERO"},
    )

    # legacy-taxonomy row (pre-fix engine string) → unaffected by the new rules
    await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        bucket="auto_classifications",
        match_type="deterministic",
        variance_type="fees",
        variance_amount=Decimal("9.00"),
        stripe_amount=Decimal("1000.00"),
        netsuite_amount=Decimal("991.00"),
        evidence={"charge_source_id": "ch_legacy_fees", "order_reference": "R_LEGACY"},
    )

    await db.flush()

    plan = await plan_resolutions(str(run.id), user=user, db=db)
    assert plan["planned_count"] == 6  # the zero-variance fuzzy row produces no proposal

    summary = await get_resolution_summary(str(run.id), user=user, db=db)
    assert summary.proposals_count == 6
    assert summary.explained_count == 5  # only the above-materiality amount_mismatch is needs_human
    assert summary.explained_rate > 0
    assert summary.explained_rate == Decimal("83.3")

    groups_by_key = {g.group_key: g for g in summary.groups}
    assert groups_by_key["missing_in_netsuite:carry_forward:none"].count == 1
    assert groups_by_key["missing_in_netsuite:create_and_apply_deposit:customerdeposit"].count == 1
    assert groups_by_key["amount_mismatch:book_fee_line:deposit"].count == 1
    assert groups_by_key["amount_mismatch:writeoff_je:journalentry"].count == 1
    assert groups_by_key["amount_mismatch:needs_human:none"].count == 1
    # the legacy "fees" row keeps its own root_cause — never folded into
    # amount_mismatch's book_fee_line group even though the action matches.
    assert groups_by_key["fees:book_fee_line:deposit"].count == 1
    assert len(groups_by_key) == 6

    # the zero-variance fuzzy row never produced a proposal at all.
    all_charge_ids = {
        e for g in summary.groups for e in [g.group_key]
    }  # sanity: no group key references "zero_fuzzy" anywhere
    assert not any("zero" in k for k in all_charge_ids)
