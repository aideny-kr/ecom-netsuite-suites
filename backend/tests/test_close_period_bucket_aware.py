"""PART ② regression: bucket-aware close_period (HITL control).

`close_period` must NOT lock an ``auto_matched`` line whose ``bucket='needs_review'``
— that is a confident match with a *material* variance that no human reviewed.
Locking it would silently bury a material discrepancy, defeating the needs_review
bucket. Lock rule:

    lock iff  status == 'approved'  OR  (status == 'auto_matched' AND bucket != 'needs_review')

The skipped count must be surfaced (response + audit payload) as
``results_left_for_review`` for transparency.
"""

from sqlalchemy import select

from app.api.v1.reconciliation import close_period
from app.models.audit import AuditEvent
from app.services.reconciliation.four_bucket_classifier import BUCKET_NEEDS_REVIEW
from tests.conftest import (
    create_test_recon_result,
    create_test_recon_run,
    create_test_user,
)


async def test_close_period_does_not_lock_needs_review_auto_matched(db, tenant_a):
    """Lock matrix across 5 results; the auto_matched+needs_review line stays."""
    user, _ = await create_test_user(db, tenant_a)

    # Run covers period 2026-04 (factory default date range 2026-04-20..24).
    run = await create_test_recon_run(db, tenant_a.id, status="completed")

    # (a) approved + matches → locks
    r_a = await create_test_recon_result(db, tenant_a.id, run.id, status="approved", bucket="matches")
    # (b) auto_matched + matches → locks
    r_b = await create_test_recon_result(db, tenant_a.id, run.id, status="auto_matched", bucket="matches")
    # (c) auto_matched + needs_review → must NOT lock (material, unreviewed)
    r_c = await create_test_recon_result(db, tenant_a.id, run.id, status="auto_matched", bucket=BUCKET_NEEDS_REVIEW)
    # (d) approved + needs_review → locks (a human single-approved it)
    r_d = await create_test_recon_result(db, tenant_a.id, run.id, status="approved", bucket=BUCKET_NEEDS_REVIEW)
    # (e) pending + needs_review → stays pending (never lockable)
    r_e = await create_test_recon_result(db, tenant_a.id, run.id, status="pending", bucket=BUCKET_NEEDS_REVIEW)
    await db.flush()

    resp = await close_period("2026-04", user=user, db=db)

    # Refresh each row from the DB to read the persisted status.
    for r in (r_a, r_b, r_c, r_d, r_e):
        await db.refresh(r)

    assert r_a.status == "locked"  # approved + matches
    assert r_b.status == "locked"  # auto_matched + matches
    assert r_c.status == "auto_matched"  # auto_matched + needs_review — NOT locked
    assert r_d.status == "locked"  # approved + needs_review (human-approved)
    assert r_e.status == "pending"  # pending + needs_review — untouched

    # Transparency: exactly one line (c) was left for review.
    assert resp["results_left_for_review"] == 1
    assert resp["results_locked"] == 3  # a, b, d
    assert resp["runs_closed"] == 1

    # The human-facing message surfaces BOTH the locked count and the
    # left-for-review count so the skipped (material, unreviewed) items are visible.
    assert "3 results locked" in resp["message"]
    assert "1 left for review" in resp["message"]

    # The run itself is closed.
    await db.refresh(run)
    assert run.status == "closed"

    # Audit payload carries the same skipped count.
    events = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "recon.close_period",
                    AuditEvent.resource_id == "2026-04",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].payload is not None
    assert events[0].payload["results_left_for_review"] == 1
