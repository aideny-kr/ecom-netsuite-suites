"""resolution-summary aggregation + per-group proposal listing."""

from decimal import Decimal

from app.api.v1.reconciliation import (
    get_resolution_summary,
    list_group_proposals,
    plan_resolutions,
    reject_resolution_group,
)
from tests.conftest import create_test_recon_result, create_test_recon_run, create_test_user


async def _seed(db, tenant):
    user, _ = await create_test_user(db, tenant)
    run = await create_test_recon_run(db, tenant.id, status="completed")
    # 2 fee lines (one above materiality), 1 timing, 1 chargeback, 1 clean match.
    # Materiality is R2a OR-semantics: above when > $50 abs OR > 1% of order.
    # $9 on $1000 (0.9%) is sub-materiality; $120 on $10000 is above ($120 > $50).
    for amt, stripe in ((Decimal("9.00"), Decimal("1000")), (Decimal("120.00"), Decimal("10000"))):
        await create_test_recon_result(
            db,
            tenant.id,
            run.id,
            status="pending",
            bucket="auto_classifications",
            match_type="deterministic",
            variance_type="fees",
            variance_amount=amt,
            stripe_amount=stripe,
            netsuite_amount=stripe - amt,
            evidence={"charge_source_id": f"ch_{amt}", "order_reference": "R1"},
        )
    await create_test_recon_result(
        db,
        tenant.id,
        run.id,
        status="pending",
        bucket="rules",
        match_type="fuzzy",
        variance_type="timing",
        variance_amount=Decimal("0"),
        stripe_amount=Decimal("50"),
        netsuite_amount=Decimal("50"),
        evidence={"charge_source_id": "ch_t", "order_reference": "R2"},
    )
    await create_test_recon_result(
        db,
        tenant.id,
        run.id,
        status="pending",
        bucket="needs_review",
        match_type="deterministic",
        variance_type="chargeback",
        variance_amount=Decimal("42"),
        stripe_amount=Decimal("42"),
        netsuite_amount=Decimal("0"),
        evidence={"charge_source_id": "ch_c", "order_reference": "R3"},
    )
    await create_test_recon_result(
        db,
        tenant.id,
        run.id,
        status="auto_matched",
        bucket="matches",
        match_type="deterministic",
        variance_type=None,
        variance_amount=Decimal("0"),
        stripe_amount=Decimal("10"),
        netsuite_amount=Decimal("10"),
        evidence={"charge_source_id": "ch_m", "order_reference": "R4"},
    )
    # In production, recon_job.py/order_recon_job.py set this from bucket counts
    # once the pipeline finishes. This test seeds results directly (no pipeline
    # run), so it must set the same invariant by hand.
    run.matches_count = 1
    await db.flush()
    await plan_resolutions(str(run.id), user=user, db=db)
    return user, run


async def test_summary_groups_and_rates(db, tenant_a):
    user, run = await _seed(db, tenant_a)
    out = await get_resolution_summary(str(run.id), user=user, db=db)

    assert out.total_results == 5
    assert out.matches_count == 1
    assert out.proposals_count == 4
    # chargeback → needs_human; fees ×2 + timing are "explained"
    assert out.explained_count == 3
    assert out.explained_rate == Decimal("75.0")
    keys = {g.group_key for g in out.groups}
    assert "fees:book_fee_line:deposit" in keys
    assert "timing:carry_forward:none" in keys
    assert "chargeback:needs_human:none" in keys
    fee_group = next(g for g in out.groups if g.root_cause == "fees")
    assert fee_group.count == 2
    assert fee_group.above_materiality_count == 1
    assert fee_group.total_amount == Decimal("129.00")
    assert out.variance_by_root_cause["fees"] == Decimal("129.00")


async def test_guard_skipped_count_excludes_human_rejected_proposals(db, tenant_a):
    """T2 gate finding: guard_skipped_count must count only results with NO
    proposal at all (never planned / guard-skipped), not results whose
    proposal a human rejected — those fall out of the live proposals_count
    but were still planned, so they must not be mislabeled as guard-skipped."""
    user, run = await _seed(db, tenant_a)
    await reject_resolution_group(str(run.id), "fees:book_fee_line:deposit", user=user, db=db)

    out = await get_resolution_summary(str(run.id), user=user, db=db)
    assert out.guard_skipped_count == 0


async def test_group_proposals_listing_paginated(db, tenant_a):
    user, run = await _seed(db, tenant_a)
    page = await list_group_proposals(str(run.id), "fees:book_fee_line:deposit", user=user, db=db, limit=1, offset=0)
    assert len(page) == 1
    assert page[0].action == "book_fee_line"


async def test_summary_404_on_foreign_run(db, tenant_a, tenant_b):
    user, _ = await create_test_user(db, tenant_a)
    run_b = await create_test_recon_run(db, tenant_b.id, status="completed")
    await db.flush()
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await get_resolution_summary(str(run_b.id), user=user, db=db)
    assert exc.value.status_code == 404
