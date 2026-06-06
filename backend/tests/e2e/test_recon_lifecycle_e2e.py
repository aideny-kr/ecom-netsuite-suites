"""Seeded-tenant reconciliation lifecycle e2e (Phase 2 — recon regression backbone).

Codifies the live R1 write-path UAT (2026-06-03, staging Framework, zero residue)
as a deterministic, CI-runnable test against the local docker Postgres. It drives
the REAL order-level engine (create-run) and the REAL HTTP write-path
(approve/close), reusing ``app/services/reconciliation/`` + ``app/api/v1/reconciliation.py``
— no reimplementation — and asserts the HITL invariants from R1 + PR #110 + PR #112.

Path used: ORDER-LEVEL (``OrderReconJob``), the live default the R1 UAT exercised.
Layer 1+2 (engine pipeline + approve invariants) seed canonical Stripe/NetSuite-shaped
rows and run the real engine, using ONLY exact-match + unmatched inputs so bucketing is
deterministic (never relying on the fuzzy tier). Layer 3 (the #112 bucket-aware close
predicate) is factory-seeded across the (status, bucket) matrix: the close logic's
contract is defined on the *persisted* (status, bucket) pair regardless of provenance —
constructing the matrix is more precise and flake-free than engine-forcing every combo,
and the engine -> bucket mapping is already proven in Layer 1.

Invariants (see docs/superpowers/plans/2026-06-05-recon-e2e-phase2.md):
  I1 engine persists correct buckets + run rollup counts
  I2 per-line audit exactly once (single + bulk, sharing correlation_id)
  I3 no NetSuite auto-post on approve/close
  I4 variance unchanged by approval
  I5 needs_review not bulk-approvable
  I6 close = hard freeze on BOTH routes (REST + chat), no new audit on rejection
  I7 bucket-aware close: lock approved + auto_matched-non-needs_review; leave material
     auto_matched+needs_review unlocked (results_left_for_review)
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from app.mcp.tools import recon_approve
from app.models.audit import AuditEvent
from app.models.canonical import NetsuitePosting
from app.models.reconciliation import ReconciliationResult, ReconciliationRun
from app.services.reconciliation.order_recon_job import OrderReconJob
from tests.conftest import (
    create_test_netsuite_posting,
    create_test_payout_line,
    create_test_recon_result,
    create_test_recon_run,
)

# Single-month window so close_period('2026-05') selects the run; charge/deposit
# dates sit inside it (the engine fetches with a ±14d buffer).
RUN_FROM = date(2026, 5, 1)
RUN_TO = date(2026, 5, 31)
PERIOD = "2026-05"
ARRIVAL = date(2026, 5, 15)
TXN = date(2026, 5, 16)

API = "/api/v1/reconciliation"


# ---------------------------------------------------------------------------
# Layer-1/2 seeding: real Stripe/NetSuite-shaped rows -> real order engine
# ---------------------------------------------------------------------------


async def _seed_match_pair(
    db,
    tenant_id,
    *,
    order_ref: str,
    source_id: str,
    charge_amount: Decimal,
    deposit_amount: Decimal,
) -> None:
    """Seed a charge (PayoutLine) + a matching NetSuite deposit sharing ``order_ref``."""
    await create_test_payout_line(
        db,
        tenant_id,
        source_id=source_id,
        amount=charge_amount,
        description=f"Framework Marketplace Order ID: {order_ref}-XU9EPZPD",
        arrival_date=ARRIVAL,
    )
    await create_test_netsuite_posting(
        db,
        tenant_id,
        netsuite_internal_id=f"ns-{source_id}",
        record_type="custdep",
        amount=deposit_amount,
        transaction_date=TXN,
        related_payout_id=order_ref,
    )


async def _seed_standard_run(db, tenant_id):
    """Seed a deterministic 4-charge scenario and run the REAL order engine.

    Produces (default $50 / 1% materiality from the seeded TenantConfig):
      - 2x exact match   -> match_type=deterministic, variance 0 -> bucket=matches,   status=auto_matched
      - 1x $150 variance -> match_type=deterministic, material   -> bucket=needs_review, status=suggested (conf 0.90)
      - 1x unmatched     -> match_type=unmatched                 -> bucket=needs_review, status=pending

    Returns the ReconRunSummary.
    """
    await _seed_match_pair(
        db,
        tenant_id,
        order_ref="R100000001",
        source_id="ch_m1",
        charge_amount=Decimal("100.00"),
        deposit_amount=Decimal("100.00"),
    )
    await _seed_match_pair(
        db,
        tenant_id,
        order_ref="R100000002",
        source_id="ch_m2",
        charge_amount=Decimal("200.00"),
        deposit_amount=Decimal("200.00"),
    )
    await _seed_match_pair(
        db,
        tenant_id,
        order_ref="R100000003",
        source_id="ch_v1",
        charge_amount=Decimal("1000.00"),
        deposit_amount=Decimal("850.00"),
    )
    # Unmatched: a charge with an order ref but no corresponding deposit.
    await create_test_payout_line(
        db,
        tenant_id,
        source_id="ch_u1",
        amount=Decimal("50.00"),
        description="Framework Marketplace Order ID: R100000004-XU9EPZPD",
        arrival_date=ARRIVAL,
    )
    return await OrderReconJob(db, str(tenant_id)).run(RUN_FROM, RUN_TO)


async def _results(db, run_id: uuid.UUID):
    return (await db.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run_id))).scalars().all()


async def _count_ns(db, tenant_id) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(NetsuitePosting).where(NetsuitePosting.tenant_id == tenant_id)
        )
    ).scalar_one()


# ---------------------------------------------------------------------------
# I1 — engine create-run persists correct buckets + rollup counts
# ---------------------------------------------------------------------------


async def test_engine_pipeline_persists_correct_buckets(db, tenant_a):
    summary = await _seed_standard_run(db, tenant_a.id)

    assert summary.status == "completed"
    assert summary.total_payouts == 4  # 4 charges
    assert summary.total_deposits == 3
    assert summary.matched_count == 3  # 2 exact + 1 variance (all deterministic)
    assert summary.unmatched_count == 1

    run_id = uuid.UUID(summary.run_id)
    rows = await _results(db, run_id)
    assert len(rows) == 4

    by_bucket = Counter(r.bucket for r in rows)
    assert by_bucket["matches"] == 2
    assert by_bucket["needs_review"] == 2  # material variance + unmatched
    assert by_bucket["auto_classifications"] == 0
    assert by_bucket["rules"] == 0

    status_by_ref = {r.evidence["order_reference"]: r.status for r in rows}
    assert status_by_ref["R100000001"] == "auto_matched"  # exact -> conf 1.0
    assert status_by_ref["R100000003"] == "suggested"  # material variance -> conf 0.90
    assert status_by_ref["R100000004"] == "pending"  # unmatched

    run = (await db.execute(select(ReconciliationRun).where(ReconciliationRun.id == run_id))).scalar_one()
    assert run.matches_count == 2
    assert run.needs_review_count == 2
    assert run.auto_classifications_count == 0
    assert run.rules_count == 0


# ---------------------------------------------------------------------------
# I2 + I3 + I4 — bulk-approve: per-line audit, no post, variance unchanged
# ---------------------------------------------------------------------------


async def test_bulk_approve_writes_per_line_audit_no_post_variance_unchanged(db, admin_user, client):
    user, headers = admin_user
    summary = await _seed_standard_run(db, user.tenant_id)
    run_id = summary.run_id
    run_uuid = uuid.UUID(run_id)

    total_var_before = (
        await db.execute(select(ReconciliationRun.total_variance).where(ReconciliationRun.id == run_uuid))
    ).scalar_one()
    ns_before = await _count_ns(db, user.tenant_id)
    matches_before = (
        (
            await db.execute(
                select(ReconciliationResult).where(
                    ReconciliationResult.run_id == run_uuid, ReconciliationResult.bucket == "matches"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(matches_before) == 2
    variance_before = {str(r.id): r.variance_amount for r in matches_before}

    resp = await client.post(f"{API}/runs/{run_id}/approve-bucket", json={"bucket": "matches"}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["approved_count"] == 2
    corr = body["correlation_id"]
    assert corr

    # I2: exactly one per-line audit per approved line + one summary, all sharing corr.
    per_line = (
        await db.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.correlation_id == corr, AuditEvent.action == "recon.approve")
        )
    ).scalar_one()
    assert per_line == 2
    summary_events = (
        await db.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.correlation_id == corr, AuditEvent.action == "recon.bulk_approve")
        )
    ).scalar_one()
    assert summary_events == 1

    matches_after = (
        (
            await db.execute(
                select(ReconciliationResult).where(
                    ReconciliationResult.run_id == run_uuid, ReconciliationResult.bucket == "matches"
                )
            )
        )
        .scalars()
        .all()
    )
    assert all(r.status == "approved" for r in matches_after)

    # I4: approval must not recompute variance (run-level or per-line).
    total_var_after = (
        await db.execute(select(ReconciliationRun.total_variance).where(ReconciliationRun.id == run_uuid))
    ).scalar_one()
    assert total_var_after == total_var_before
    assert {str(r.id): r.variance_amount for r in matches_after} == variance_before

    # I3: no NetSuite auto-post — no NS rows created/removed, and no non-approve audit
    # action (a post would create an NS record and/or a posting audit event).
    assert await _count_ns(db, user.tenant_id) == ns_before
    foreign_actions = (
        await db.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.correlation_id == corr,
                AuditEvent.action.notin_(["recon.approve", "recon.bulk_approve"]),
            )
        )
    ).scalar_one()
    assert foreign_actions == 0


# ---------------------------------------------------------------------------
# I5 — needs_review is never bulk-approvable
# ---------------------------------------------------------------------------


async def test_needs_review_bucket_not_bulk_approvable(db, admin_user, client):
    user, headers = admin_user
    summary = await _seed_standard_run(db, user.tenant_id)

    resp = await client.post(
        f"{API}/runs/{summary.run_id}/approve-bucket", json={"bucket": "needs_review"}, headers=headers
    )
    assert resp.status_code == 400, resp.text
    assert "not bulk-approvable" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# I2 + I3 — single approve: exactly one audit row, no post
# ---------------------------------------------------------------------------


async def test_single_approve_writes_exactly_one_audit_no_post(db, admin_user, client):
    user, headers = admin_user
    summary = await _seed_standard_run(db, user.tenant_id)
    run_uuid = uuid.UUID(summary.run_id)

    line = (
        (
            await db.execute(
                select(ReconciliationResult).where(
                    ReconciliationResult.run_id == run_uuid, ReconciliationResult.bucket == "matches"
                )
            )
        )
        .scalars()
        .first()
    )
    rid = str(line.id)
    ns_before = await _count_ns(db, user.tenant_id)

    resp = await client.patch(f"{API}/results/{rid}/approve", json={"result_id": rid}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"

    n = (
        await db.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "recon.approve", AuditEvent.resource_id == rid)
        )
    ).scalar_one()
    assert n == 1
    assert await _count_ns(db, user.tenant_id) == ns_before


# ---------------------------------------------------------------------------
# I7 — bucket-aware close (#112): lock eligible, leave material needs_review unlocked
# ---------------------------------------------------------------------------


async def _seed_material_nr_result(db, tenant_id, run_id, *, status):
    """A confident match with a MATERIAL variance -> status as given, bucket=needs_review."""
    return await create_test_recon_result(
        db,
        tenant_id,
        run_id,
        status=status,
        match_type="deterministic",
        variance_type="amount_mismatch",
        variance_amount=Decimal("150.00"),
        stripe_amount=Decimal("1000.00"),
        netsuite_amount=Decimal("850.00"),
        bucket="needs_review",
    )


async def test_close_period_bucket_aware_lock(db, admin_user, client):
    user, headers = admin_user
    run = await create_test_recon_run(db, user.tenant_id, status="completed", date_from=RUN_FROM, date_to=RUN_TO)

    r_approved = await create_test_recon_result(db, user.tenant_id, run.id, status="approved", bucket="matches")
    r_am_matches = await create_test_recon_result(db, user.tenant_id, run.id, status="auto_matched", bucket="matches")
    r_am_nr = await _seed_material_nr_result(db, user.tenant_id, run.id, status="auto_matched")
    r_sug_nr = await _seed_material_nr_result(db, user.tenant_id, run.id, status="suggested")
    r_pending = await create_test_recon_result(
        db, user.tenant_id, run.id, status="pending", bucket="auto_classifications"
    )

    resp = await client.post(f"{API}/close/{PERIOD}", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["runs_closed"] == 1
    assert body["results_locked"] == 2  # approved + auto_matched/matches
    assert body["results_left_for_review"] == 1  # only auto_matched + needs_review

    async def status_of(r):
        return (
            await db.execute(select(ReconciliationResult.status).where(ReconciliationResult.id == r.id))
        ).scalar_one()

    assert await status_of(r_approved) == "locked"
    assert await status_of(r_am_matches) == "locked"
    assert await status_of(r_am_nr) == "auto_matched"  # material -> left UNLOCKED
    assert await status_of(r_sug_nr) == "suggested"  # not auto_matched -> untouched
    assert await status_of(r_pending) == "pending"

    assert (
        await db.execute(select(ReconciliationRun.status).where(ReconciliationRun.id == run.id))
    ).scalar_one() == "closed"

    close_events = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.tenant_id == user.tenant_id, AuditEvent.action == "recon.close_period"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(close_events) == 1
    assert close_events[0].payload["results_left_for_review"] == 1


# ---------------------------------------------------------------------------
# I6 — close = hard freeze on BOTH routes, with no new audit on the rejection
# ---------------------------------------------------------------------------


async def test_approve_rejected_after_close_on_both_routes_no_new_audit(db, admin_user, client):
    user, headers = admin_user
    run = await create_test_recon_run(db, user.tenant_id, status="completed", date_from=RUN_FROM, date_to=RUN_TO)
    # A material auto_matched+needs_review line: deliberately left UNLOCKED on close,
    # so without the hard-freeze guard it could still be approved post-close.
    line = await _seed_material_nr_result(db, user.tenant_id, run.id, status="auto_matched")
    rid = str(line.id)

    resp = await client.post(f"{API}/close/{PERIOD}", headers=headers)
    assert resp.status_code == 200, resp.text
    # Still unlocked after close (proves the line is genuinely approvable-by-status).
    assert (
        await db.execute(select(ReconciliationResult.status).where(ReconciliationResult.id == line.id))
    ).scalar_one() == "auto_matched"

    async def approve_audits():
        return (
            await db.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "recon.approve", AuditEvent.resource_id == rid)
            )
        ).scalar_one()

    assert await approve_audits() == 0

    # REST route -> 400 hard freeze
    rest = await client.patch(f"{API}/results/{rid}/approve", json={"result_id": rid}, headers=headers)
    assert rest.status_code == 400
    assert "closed" in rest.json()["detail"].lower()

    # Chat MCP route -> error hard freeze
    chat = await recon_approve.execute({"result_id": rid}, db=db, tenant_id=user.tenant_id, user_id=user.id)
    assert chat["success"] is False
    assert "closed" in chat["error"].lower()

    # Neither rejected attempt wrote an audit row, and the line never flipped.
    assert await approve_audits() == 0
    assert (
        await db.execute(select(ReconciliationResult.status).where(ReconciliationResult.id == line.id))
    ).scalar_one() == "auto_matched"
