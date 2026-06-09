"""Seeded-tenant reconciliation lifecycle e2e (Phase 2 — recon regression backbone).

Codifies the live R1 write-path UAT (2026-06-03, staging Framework, zero residue)
as a deterministic, CI-runnable test against the local docker Postgres. It drives
the REAL order-level engine (create-run) and the REAL HTTP write-path
(approve/close), reusing ``app/services/reconciliation/`` + ``app/api/v1/reconciliation.py``
— no reimplementation — and asserts the HITL invariants from R1 + PR #110 + PR #112.

Path used: ORDER-LEVEL (``OrderReconJob``), the live default the R1 UAT exercised.
Every test seeds canonical Stripe/NetSuite-shaped rows and runs the REAL engine,
using ONLY exact-match + unmatched inputs so bucketing is deterministic (never the
fuzzy tier). Materiality is set EXPLICITLY per test (not inherited from a default),
so bucketing is hermetic to TenantConfig default changes.

The marquee #112 invariant (a material ``auto_matched`` + ``needs_review`` line is
left UNLOCKED on close) is proven against a row the **engine actually produced** —
not a hand-built factory row — by lowering materiality so a sub-tolerance variance
is material. (The factory lock-matrix across all (status,bucket) combos is already
covered by the unit test ``tests/test_close_period_bucket_aware.py``; this e2e adds
engine provenance + the HTTP path + the both-routes post-close freeze.)

Invariants (authoritative list is inline below; design rationale lives in the
gitignored plan docs/superpowers/plans/2026-06-05-recon-e2e-phase2.md):
  I1 engine persists correct buckets + run rollup counts
  I2 per-line audit exactly once (single + bulk, sharing correlation_id)
  I3 no NetSuite auto-post on approve/close
  I4 variance unchanged by approval
  I5 needs_review not bulk-approvable
  I6 close = hard freeze on BOTH routes (REST + chat), no new audit on rejection
  I7 bucket-aware close (#112): lock approved + auto_matched-non-needs_review; leave
     ENGINE-PRODUCED material auto_matched+needs_review unlocked (results_left_for_review)
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
from app.models.tenant import TenantConfig
from app.services.reconciliation.order_recon_job import OrderReconJob
from tests.conftest import create_test_netsuite_posting, create_test_payout_line

# Single-month window so close_period('2026-05') selects the run; charge/deposit
# dates sit inside it (the engine fetches with a ±14d buffer).
RUN_FROM = date(2026, 5, 1)
RUN_TO = date(2026, 5, 31)
PERIOD = "2026-05"
ARRIVAL = date(2026, 5, 15)
TXN = date(2026, 5, 16)

API = "/api/v1/reconciliation"

# Order refs below are "R" + exactly 9 digits so they match the engine's
# DEFAULT_ORDER_REF_PATTERN (R\d{9}); the seeded payout-line descriptions embed them.
_DEFAULT_MATERIALITY_ABS = Decimal("50")
_DEFAULT_MATERIALITY_PCT = Decimal("0.01")  # 1%


# ---------------------------------------------------------------------------
# Seeding helpers: real Stripe/NetSuite-shaped rows -> real order engine
# ---------------------------------------------------------------------------


async def _set_materiality(db, tenant_id, *, abs_: Decimal, pct: Decimal) -> None:
    """Pin this tenant's recon materiality thresholds so bucketing is hermetic."""
    cfg = (await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_id))).scalar_one()
    cfg.recon_materiality_abs = abs_
    cfg.recon_materiality_pct = pct
    await db.flush()


async def _seed_match_pair(
    db,
    tenant_id,
    *,
    order_ref: str,
    source_id: str,
    charge_amount: Decimal,
    deposit_amount: Decimal,
    arrival: date = ARRIVAL,
    txn: date = TXN,
) -> None:
    """Seed a charge (PayoutLine) + a matching NetSuite deposit sharing ``order_ref``.

    ``arrival`` / ``txn`` default to the module-level ARRIVAL / TXN constants so all
    existing callers are unaffected; I8 passes explicit dates to drive the temporal signal.
    """
    await create_test_payout_line(
        db,
        tenant_id,
        source_id=source_id,
        amount=charge_amount,
        description=f"Framework Marketplace Order ID: {order_ref}-XU9EPZPD",
        arrival_date=arrival,
    )
    await create_test_netsuite_posting(
        db,
        tenant_id,
        netsuite_internal_id=f"ns-{source_id}",
        record_type="custdep",
        amount=deposit_amount,
        transaction_date=txn,
        related_payout_id=order_ref,
    )


async def _seed_standard_run(db, tenant_id):
    """Seed a deterministic 4-charge scenario and run the REAL order engine.

    Materiality pinned to $50 / 1%. Produces:
      - 2x exact match   -> match_type=deterministic, variance 0 -> bucket=matches,    status=auto_matched
      - 1x $150 variance -> match_type=deterministic, material    -> bucket=needs_review, status=suggested (conf 0.90)
      - 1x unmatched     -> match_type=unmatched                  -> bucket=needs_review, status=pending

    Returns the ReconRunSummary.
    """
    await _set_materiality(db, tenant_id, abs_=_DEFAULT_MATERIALITY_ABS, pct=_DEFAULT_MATERIALITY_PCT)
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
    # $150 variance on a $1000 match -> material ($150 > $50) -> needs_review, conf 0.90 -> suggested.
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


async def _seed_engine_material_run(db, tenant_id):
    """Drive the REAL engine to emit a status=auto_matched + bucket=needs_review row.

    With materiality_abs lowered to $0.10, a deterministic match carrying a $0.30
    variance is *material* ($0.30 > $0.10) -> bucket=needs_review, while the variance
    stays inside the engine's $0.50 amount tolerance -> confidence 0.95 -> status
    auto_matched. That is the exact (status, bucket) pair the #112 close branch
    protects, now produced by the engine rather than a factory (provenance). Plus one
    exact match (-> matches, auto_matched) that close should lock. Returns the summary.
    """
    await _set_materiality(db, tenant_id, abs_=Decimal("0.10"), pct=_DEFAULT_MATERIALITY_PCT)
    await _seed_match_pair(
        db,
        tenant_id,
        order_ref="R200000001",
        source_id="ch_x1",
        charge_amount=Decimal("100.00"),
        deposit_amount=Decimal("100.00"),
    )
    await _seed_match_pair(
        db,
        tenant_id,
        order_ref="R200000002",
        source_id="ch_x2",
        charge_amount=Decimal("100.00"),
        deposit_amount=Decimal("100.30"),
    )
    return await OrderReconJob(db, str(tenant_id)).run(RUN_FROM, RUN_TO)


async def _matches(db, run_uuid):
    """The run's 'matches'-bucket rows, ordered deterministically by stripe_amount."""
    return (
        (
            await db.execute(
                select(ReconciliationResult)
                .where(
                    ReconciliationResult.run_id == run_uuid,
                    ReconciliationResult.bucket == "matches",
                )
                .order_by(ReconciliationResult.stripe_amount)
            )
        )
        .scalars()
        .all()
    )


async def _status_of(db, result_id):
    return (
        await db.execute(select(ReconciliationResult.status).where(ReconciliationResult.id == result_id))
    ).scalar_one()


async def _approve_audit_count(db, rid: str) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "recon.approve", AuditEvent.resource_id == rid)
        )
    ).scalar_one()


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
    rows = (await db.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run_id))).scalars().all()
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
    run_uuid = uuid.UUID(summary.run_id)

    total_var_before = (
        await db.execute(select(ReconciliationRun.total_variance).where(ReconciliationRun.id == run_uuid))
    ).scalar_one()
    ns_before = await _count_ns(db, user.tenant_id)
    matches_before = await _matches(db, run_uuid)
    assert len(matches_before) == 2
    variance_before = {str(r.id): r.variance_amount for r in matches_before}

    resp = await client.post(f"{API}/runs/{summary.run_id}/approve-bucket", json={"bucket": "matches"}, headers=headers)
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

    matches_after = await _matches(db, run_uuid)
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

    # Deterministic target: the smallest-amount matches line (R100000001, $100).
    line = (await _matches(db, run_uuid))[0]
    rid = str(line.id)
    ns_before = await _count_ns(db, user.tenant_id)

    resp = await client.patch(f"{API}/results/{rid}/approve", json={"result_id": rid}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"

    assert await _approve_audit_count(db, rid) == 1
    assert await _count_ns(db, user.tenant_id) == ns_before


# ---------------------------------------------------------------------------
# I7 — bucket-aware close (#112) with ENGINE PROVENANCE:
#   the engine produces a material auto_matched+needs_review row; close leaves it unlocked.
# ---------------------------------------------------------------------------


async def test_engine_material_match_auto_matched_needs_review_survives_close(db, admin_user, client):
    user, headers = admin_user
    summary = await _seed_engine_material_run(db, user.tenant_id)
    run_uuid = uuid.UUID(summary.run_id)

    rows = {
        r.evidence["order_reference"]: r
        for r in (await db.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run_uuid)))
        .scalars()
        .all()
    }
    material = rows["R200000002"]
    exact = rows["R200000001"]
    # Provenance: the marquee #112 pair is emitted by the REAL engine, not a factory.
    assert (material.status, material.bucket) == ("auto_matched", "needs_review")
    assert (exact.status, exact.bucket) == ("auto_matched", "matches")

    resp = await client.post(f"{API}/close/{PERIOD}", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["runs_closed"] == 1
    assert body["results_locked"] == 1  # auto_matched + matches
    assert body["results_left_for_review"] == 1  # the engine-produced auto_matched + needs_review

    assert await _status_of(db, material.id) == "auto_matched"  # material discrepancy LEFT UNLOCKED
    assert await _status_of(db, exact.id) == "locked"
    assert (
        await db.execute(select(ReconciliationRun.status).where(ReconciliationRun.id == run_uuid))
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
    summary = await _seed_engine_material_run(db, user.tenant_id)
    run_uuid = uuid.UUID(summary.run_id)

    # The engine-produced material line (auto_matched + needs_review) is deliberately
    # left UNLOCKED on close, so without the hard-freeze guard it could still be approved.
    material = (
        (
            await db.execute(
                select(ReconciliationResult).where(
                    ReconciliationResult.run_id == run_uuid,
                    ReconciliationResult.bucket == "needs_review",
                )
            )
        )
        .scalars()
        .one()
    )
    rid = str(material.id)

    resp = await client.post(f"{API}/close/{PERIOD}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert await _status_of(db, material.id) == "auto_matched"  # still unlocked in the closed run
    assert await _approve_audit_count(db, rid) == 0

    # REST route -> 400 hard freeze
    rest = await client.patch(f"{API}/results/{rid}/approve", json={"result_id": rid}, headers=headers)
    assert rest.status_code == 400
    assert "closed" in rest.json()["detail"].lower()

    # Chat MCP route -> error hard freeze
    chat = await recon_approve.execute({"result_id": rid}, db=db, tenant_id=user.tenant_id, user_id=user.id)
    assert chat["success"] is False
    assert "closed" in chat["error"].lower()

    # Neither rejected attempt wrote an audit row, and the line never flipped.
    assert await _approve_audit_count(db, rid) == 0
    assert await _status_of(db, material.id) == "auto_matched"


# ---------------------------------------------------------------------------
# I8 — R2 advisory confidence: calibrated score + signals persisted; status/bucket
#       decoupled (both pairs are auto_matched+matches despite different confidence)
# ---------------------------------------------------------------------------


async def test_engine_persists_calibrated_confidence_and_signals(db, tenant_a):
    """I8 — The R2 scorer wires into the write-path with decoupled status/bucket.

    Two exact-amount pairs differ ONLY in date gap (0 days vs 14 days). Both are
    deterministic matches (conf ladder 1.0/0.95) so status=auto_matched, bucket=matches
    for both — the engine match-tier value still drives status/close-lock as before.

    The persisted ``confidence`` column now carries the R2 advisory composite score:
      - Same-day  (gap 0 ):  amount_score=1.0, temporal_score=1.0 → composite=1.0
      - Far pair  (gap 14):  amount_score=1.0, temporal_score=0.0 → composite=0.6

    This proves the two-value decoupling: status/bucket identical; confidence diverges.
    The ``evidence["confidence_signals"]`` sub-dict makes the signals observable/auditable.
    """
    await _set_materiality(db, tenant_a.id, abs_=_DEFAULT_MATERIALITY_ABS, pct=_DEFAULT_MATERIALITY_PCT)

    # Same-day pair: charge arrival == NS transaction date  (gap = 0)
    await _seed_match_pair(
        db,
        tenant_a.id,
        order_ref="R300000001",
        source_id="ch_s",
        charge_amount=Decimal("100.00"),
        deposit_amount=Decimal("100.00"),
        arrival=date(2026, 5, 16),
        txn=date(2026, 5, 16),
    )
    # Far pair: 14-day gap between arrival and NS transaction date
    await _seed_match_pair(
        db,
        tenant_a.id,
        order_ref="R300000002",
        source_id="ch_f",
        charge_amount=Decimal("100.00"),
        deposit_amount=Decimal("100.00"),
        arrival=date(2026, 5, 2),
        txn=date(2026, 5, 16),
    )

    summary = await OrderReconJob(db, str(tenant_a.id)).run(RUN_FROM, RUN_TO)
    assert summary.status == "completed"

    run_uuid = uuid.UUID(summary.run_id)
    rows = (
        (await db.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run_uuid))).scalars().all()
    )
    assert len(rows) == 2

    by_ref = {r.evidence["order_reference"]: r for r in rows}
    same_day = by_ref["R300000001"]
    far = by_ref["R300000002"]

    # ---- Decoupling: status + bucket are IDENTICAL despite different confidence ----
    assert same_day.status == "auto_matched"
    assert same_day.bucket == "matches"
    assert far.status == "auto_matched"
    assert far.bucket == "matches"

    # ---- R2 advisory composite persisted (calibrated by amount + temporal signals) ----
    # The Decimal(...) literals below are deliberate regression tripwires — pinned, NOT
    # computed from the engine constants. The amount-only fallback (temporal_score is None)
    # is NOT reachable through OrderReconJob (deposits are fetched with a non-null
    # transaction_date filter), so it's covered only by the confidence_engine unit tests
    # (Task A), never here.
    # gap 0 → temporal 1.0 → composite 1.0000
    assert same_day.confidence == Decimal("1.0000"), f"same-day confidence: {same_day.confidence}"
    # 0.6*amount_score(1.0) + 0.4*temporal_score(gap 14 == WINDOW_DAYS → 0.0) = 0.6000
    # (W_AMOUNT/W_TEMPORAL/WINDOW_DAYS live in confidence_engine.py — if those change,
    # update this literal deliberately)
    assert far.confidence == Decimal("0.6000"), f"far confidence: {far.confidence}"

    # ---- confidence_signals sub-dict present with correct keys + values ----
    for row, label in ((same_day, "same-day"), (far, "far")):
        sigs = row.evidence.get("confidence_signals")
        assert sigs is not None, f"{label}: confidence_signals missing from evidence"
        for key in ("amount_score", "temporal_score", "composite", "scorer_version", "weights"):
            assert key in sigs, f"{label}: missing key {key!r} in confidence_signals"
        assert sigs["scorer_version"] == "v1", f"{label}: scorer_version mismatch"
        assert sigs["weights"] == {"amount": "0.6", "temporal": "0.4"}, f"{label}: weights mismatch"

    # Same-day: temporal=1.0, composite=1.0
    assert same_day.evidence["confidence_signals"]["temporal_score"] == "1.0000", (
        f"same-day temporal_score: {same_day.evidence['confidence_signals']['temporal_score']}"
    )
    assert same_day.evidence["confidence_signals"]["composite"] == "1.0000", (
        f"same-day composite: {same_day.evidence['confidence_signals']['composite']}"
    )

    # Far pair: temporal=0.0, composite=0.6
    assert far.evidence["confidence_signals"]["temporal_score"] == "0.0000", (
        f"far temporal_score: {far.evidence['confidence_signals']['temporal_score']}"
    )
    assert far.evidence["confidence_signals"]["composite"] == "0.6000", (
        f"far composite: {far.evidence['confidence_signals']['composite']}"
    )

    # ---- Original evidence keys still present ----
    for row, label in ((same_day, "same-day"), (far, "far")):
        ev = row.evidence
        assert "charge_source_id" in ev, f"{label}: charge_source_id missing"
        assert "order_reference" in ev, f"{label}: order_reference missing"
        assert "charge_payout_line_id" in ev, f"{label}: charge_payout_line_id missing"
