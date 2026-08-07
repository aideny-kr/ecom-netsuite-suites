"""carried_forward is terminal, non-blocking for close, and never locked."""

from decimal import Decimal

from app.api.v1.reconciliation import approve_bucket, close_period, get_close_readiness
from app.schemas.reconciliation import ReconBucketApprove
from tests.conftest import create_test_recon_result, create_test_recon_run, create_test_user


async def test_carried_forward_unblocks_readiness_and_is_not_locked(db, tenant_a):
    user, _ = await create_test_user(db, tenant_a)
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    r = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="carried_forward",
        bucket="rules",
        match_type="fuzzy",
        variance_type="timing",
        variance_amount=Decimal("0"),
    )
    await db.flush()

    readiness = await get_close_readiness("2026-04", user=user, db=db)
    assert readiness.open_exceptions == 0  # not pending → not blocking
    assert readiness.carried_forward == 1  # visible as its own count

    resp = await close_period("2026-04", user=user, db=db)
    await db.refresh(r)
    assert r.status == "carried_forward"  # never locked
    assert resp["results_locked"] == 0


async def test_bulk_approve_skips_carried_forward(db, tenant_a):
    user, _ = await create_test_user(db, tenant_a)
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    r = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="carried_forward",
        bucket="rules",
        match_type="fuzzy",
        variance_type="timing",
        variance_amount=Decimal("0"),
    )
    await db.flush()
    out = await approve_bucket(str(run.id), ReconBucketApprove(bucket="rules"), user=user, db=db)
    await db.refresh(r)
    assert r.status == "carried_forward"  # TERMINAL_RESULT_STATUSES skip
    assert out.approved_count == 0


async def test_rejected_rows_stay_visible_on_the_close_checklist(db, tenant_a):
    """Reject must not be a way to make unresolved variance disappear from close.

    Reject OVERWRITES 'pending'/'auto_matched', the two statuses close-readiness
    counts. Without a counter of its own, a reviewer could zero the checklist by
    rejecting rows and the variance would leave the close report with no trace —
    measured at $1,150 over two rows. Rejected is decided, so it does not BLOCK;
    it must still be visible, exactly like carried_forward.
    """
    from app.services.reconciliation.recon_reject import reject_result

    user, _ = await create_test_user(db, tenant_a)
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    pending = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        match_type="fuzzy",
        variance_type="amount_mismatch",
        variance_amount=Decimal("250"),
    )
    await db.flush()

    before = await get_close_readiness("2026-04", user=user, db=db)
    assert before.open_exceptions == 1

    await reject_result(db, result=pending, user=user, reason="not_actionable", note="vendor dispute", run=run)
    await db.flush()

    after = await get_close_readiness("2026-04", user=user, db=db)
    assert after.open_exceptions == 0, "decided, so it no longer blocks"
    assert after.rejected == 1, "but it must remain VISIBLE on the checklist"
