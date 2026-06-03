import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.audit import AuditEvent
from app.models.reconciliation import ReconciliationResult
from app.services.reconciliation.four_bucket_classifier import (
    ALL_BUCKETS,
    bucket_conditions,
    classify,
)
from tests.conftest import create_test_recon_result, create_test_recon_run


async def test_factories_seed_run_and_result(db, tenant_a):
    run = await create_test_recon_run(db, tenant_a.id)
    res = await create_test_recon_result(db, tenant_a.id, run.id, match_type="fuzzy")
    await db.flush()
    assert res.run_id == run.id
    assert res.tenant_id == tenant_a.id
    assert res.status == "suggested"


# (match_type, variance_type, variance_amount)
_MATRIX = [
    ("deterministic", None, Decimal("0")),
    ("deterministic", "amount_mismatch", Decimal("0.12")),
    ("deterministic", None, Decimal("5.00")),
    ("fuzzy", None, Decimal("0")),
    ("fuzzy", "amount_mismatch", Decimal("5.11")),
    ("unmatched", "missing_in_netsuite", Decimal("1203.68")),
    ("exception", "duplicate", Decimal("0")),
    ("unmatched", "missing", Decimal("0")),
]


async def test_sql_twin_partitions_identically_to_classify(db, tenant_a):
    run = await create_test_recon_run(db, tenant_a.id)
    seeded = []
    for mt, vt, va in _MATRIX:
        r = await create_test_recon_result(db, tenant_a.id, run.id, match_type=mt, variance_type=vt, variance_amount=va)
        seeded.append(r)
    await db.flush()

    # Every seeded row appears in exactly one bucket via the SQL twin, and that
    # bucket equals classify() for the same row.
    seen: dict = {}
    for bucket in ALL_BUCKETS:
        rows = (
            (
                await db.execute(
                    select(ReconciliationResult.id).where(
                        ReconciliationResult.run_id == run.id,
                        bucket_conditions(bucket),
                    )
                )
            )
            .scalars()
            .all()
        )
        for rid in rows:
            assert rid not in seen, "row matched two buckets"
            seen[rid] = bucket

    assert len(seen) == len(seeded), "row matched zero buckets"
    for r in seeded:
        py_bucket = classify(r.match_type, r.variance_type, r.variance_amount)
        assert seen[r.id] == py_bucket


def test_bucket_conditions_rejects_unknown_bucket():
    with pytest.raises(ValueError):
        bucket_conditions("not_a_bucket")


# ---------------------------------------------------------------------------
# Task 5: GET /runs/{run_id}/buckets — authoritative per-bucket counts + variance
# ---------------------------------------------------------------------------


async def _enable_recon(db, tenant_id):
    """Enable the reconciliation feature flag (defaults off) for HTTP tests."""
    from app.services.feature_flag_service import clear_cache, set_flag

    clear_cache()
    await set_flag(db, tenant_id, "reconciliation", True)
    await db.flush()
    clear_cache()


async def _seed_one_run_per_bucket(db, tenant_id):
    run = await create_test_recon_run(db, tenant_id)
    # 2 matches, 1 rule, 3 auto-classifications, 2 needs-review
    await create_test_recon_result(db, tenant_id, run.id, match_type="deterministic")
    await create_test_recon_result(db, tenant_id, run.id, match_type="deterministic")
    await create_test_recon_result(db, tenant_id, run.id, match_type="fuzzy", confidence=Decimal("0.85"))
    for amt in ("0.12", "4.12", "5.00"):
        await create_test_recon_result(
            db,
            tenant_id,
            run.id,
            match_type="deterministic",
            variance_type="amount_mismatch",
            variance_amount=Decimal(amt),
        )
    await create_test_recon_result(
        db,
        tenant_id,
        run.id,
        match_type="unmatched",
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("100"),
        status="pending",
    )
    await create_test_recon_result(
        db,
        tenant_id,
        run.id,
        match_type="unmatched",
        variance_type="missing",
        status="pending",
    )
    await db.commit()
    return run


async def test_bucket_summary_counts(client, db, finance_user):
    user, headers = finance_user
    await _enable_recon(db, user.tenant_id)
    run = await _seed_one_run_per_bucket(db, user.tenant_id)
    resp = await client.get(f"/api/v1/reconciliation/runs/{run.id}/buckets", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["matches"]["count"] == 2
    assert body["rules"]["count"] == 1
    assert body["auto_classifications"]["count"] == 3
    assert body["needs_review"]["count"] == 2
    # auto-classifications total variance = 0.12 + 4.12 + 5.00
    assert Decimal(str(body["auto_classifications"]["total_variance"])) == Decimal("9.24")


# ---------------------------------------------------------------------------
# Task 6: bucket filter param on get_run_results
# ---------------------------------------------------------------------------


async def test_get_results_filtered_by_bucket(client, db, finance_user):
    user, headers = finance_user
    await _enable_recon(db, user.tenant_id)
    run = await _seed_one_run_per_bucket(db, user.tenant_id)
    resp = await client.get(
        f"/api/v1/reconciliation/runs/{run.id}/results?bucket=auto_classifications&limit=100",
        headers=headers,
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 3
    assert all(r["bucket"] == "auto_classifications" for r in rows)


async def test_get_results_invalid_bucket_is_422(client, db, finance_user):
    user, headers = finance_user
    await _enable_recon(db, user.tenant_id)
    run = await _seed_one_run_per_bucket(db, user.tenant_id)
    resp = await client.get(f"/api/v1/reconciliation/runs/{run.id}/results?bucket=nope", headers=headers)
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# Task 7: POST /runs/{run_id}/approve-bucket — set-based bulk approve
# ---------------------------------------------------------------------------


async def test_bulk_approve_matches_emits_per_line_audit(client, db, finance_user):
    user, headers = finance_user
    run = await _seed_one_run_per_bucket(db, user.tenant_id)  # 2 matches

    resp = await client.post(
        f"/api/v1/reconciliation/runs/{run.id}/approve-bucket",
        json={"bucket": "matches", "notes": "Q2 close"},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["approved_count"] == 2
    assert body["skipped_count"] == 0
    corr = body["correlation_id"]

    # statuses flipped
    statuses = (
        (
            await db.execute(
                select(ReconciliationResult.status).where(
                    ReconciliationResult.run_id == run.id,
                    bucket_conditions("matches"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert statuses == ["approved", "approved"]

    # one per-line audit event per approved line + one summary event, sharing correlation_id
    per_line = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "recon.approve",
                    AuditEvent.correlation_id == corr,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(per_line) == 2
    assert {e.resource_type for e in per_line} == {"reconciliation_result"}

    summary = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "recon.bulk_approve",
                    AuditEvent.correlation_id == corr,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(summary) == 1
    assert summary[0].resource_type == "reconciliation_run"
    assert summary[0].payload["bucket"] == "matches"
    assert summary[0].payload["approved_count"] == 2


async def test_bulk_approve_skips_locked_and_already_approved(client, db, finance_user):
    user, headers = finance_user
    run = await create_test_recon_run(db, user.tenant_id)
    await create_test_recon_result(db, user.tenant_id, run.id, match_type="deterministic", status="approved")
    await create_test_recon_result(db, user.tenant_id, run.id, match_type="deterministic", status="locked")
    await create_test_recon_result(db, user.tenant_id, run.id, match_type="deterministic", status="suggested")
    await db.commit()

    resp = await client.post(
        f"/api/v1/reconciliation/runs/{run.id}/approve-bucket",
        json={"bucket": "matches"},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["approved_count"] == 1  # only the suggested one
    assert body["skipped_count"] == 2  # approved + locked untouched


async def test_bulk_approve_rejects_needs_review(client, db, finance_user):
    user, headers = finance_user
    run = await create_test_recon_run(db, user.tenant_id)
    resp = await client.post(
        f"/api/v1/reconciliation/runs/{run.id}/approve-bucket",
        json={"bucket": "needs_review"},
        headers=headers,
    )
    assert resp.status_code == 400


async def test_bulk_approve_requires_permission(client, db, readonly_user):
    user, headers = readonly_user
    run = await create_test_recon_run(db, user.tenant_id)
    resp = await client.post(
        f"/api/v1/reconciliation/runs/{run.id}/approve-bucket",
        json={"bucket": "matches"},
        headers=headers,
    )
    assert resp.status_code == 403


async def test_bulk_approve_unknown_run_404(client, db, finance_user):
    user, headers = finance_user
    resp = await client.post(
        f"/api/v1/reconciliation/runs/{uuid.uuid4()}/approve-bucket",
        json={"bucket": "matches"},
        headers=headers,
    )
    assert resp.status_code == 404


async def test_bulk_approve_rejects_closed_period(client, db, finance_user):
    """A closed/locked period must reject bulk approve; rows stay un-flipped."""
    user, headers = finance_user
    run = await create_test_recon_run(db, user.tenant_id, status="closed")
    res = await create_test_recon_result(db, user.tenant_id, run.id, match_type="deterministic", status="suggested")
    await db.commit()

    resp = await client.post(
        f"/api/v1/reconciliation/runs/{run.id}/approve-bucket",
        json={"bucket": "matches"},
        headers=headers,
    )
    assert resp.status_code == 400

    refreshed = (
        await db.execute(select(ReconciliationResult.status).where(ReconciliationResult.id == res.id))
    ).scalar_one()
    assert refreshed == "suggested"


# ---------------------------------------------------------------------------
# Malformed-id 404 (consistent _parse_uuid across read + single-approve)
# ---------------------------------------------------------------------------


async def test_get_results_malformed_run_id_404(client, db, finance_user):
    user, headers = finance_user
    await _enable_recon(db, user.tenant_id)
    resp = await client.get(
        "/api/v1/reconciliation/runs/not-a-uuid/results?bucket=matches",
        headers=headers,
    )
    assert resp.status_code == 404


async def test_approve_result_malformed_id_404(client, db, finance_user):
    user, headers = finance_user
    resp = await client.patch(
        "/api/v1/reconciliation/results/not-a-uuid/approve",
        json={"result_id": str(uuid.uuid4()), "notes": None},
        headers=headers,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Bucket summary must 404 on an unknown/foreign run (not return all-zeros 200)
# ---------------------------------------------------------------------------


async def test_bucket_summary_unknown_run_404(client, db, finance_user):
    user, headers = finance_user
    await _enable_recon(db, user.tenant_id)
    resp = await client.get(
        f"/api/v1/reconciliation/runs/{uuid.uuid4()}/buckets",
        headers=headers,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Single-line approve must reject locked (period-closed) rows
# ---------------------------------------------------------------------------


async def test_approve_single_locked_result_400(client, db, finance_user):
    user, headers = finance_user
    run = await create_test_recon_run(db, user.tenant_id)
    res = await create_test_recon_result(db, user.tenant_id, run.id, match_type="deterministic", status="locked")
    await db.commit()

    resp = await client.patch(
        f"/api/v1/reconciliation/results/{res.id}/approve",
        json={"result_id": str(res.id), "notes": None},
        headers=headers,
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# R2a T5 — materiality routing on the read/approve surface
#
# After R2a the bucket is PERSISTED (compute-at-write via classify() with the
# tenant's materiality thresholds), and bucket_conditions() filters on that stored
# ``bucket`` column. A matched (deterministic|fuzzy) line whose variance is
# *material* ($50 OR 1%) is stored with ``bucket='needs_review'`` even though its
# ``match_type`` stays 'deterministic'. The reviewer surface must (a) surface it
# in needs_review (not matches/auto_classifications) and (b) keep it out of
# bulk-approve — needs_review ∉ BULK_APPROVABLE_BUCKETS, so it is structurally
# excluded.
#
# These tests seed the stored bucket directly (the factory mirrors production via
# classify(), with an explicit override for the material case). They are DB-backed
# (conftest ``db`` fixture / local docker Postgres) and were written but NOT run in
# the implementer environment — the PM runs them post-flight.
# ---------------------------------------------------------------------------


async def _seed_material_matched_row(db, tenant_id, run_id):
    """A deterministic, *matched* line whose variance is material → stored needs_review.

    Materiality is computed at write-time, so we seed the stored bucket explicitly
    (``bucket='needs_review'``) while keeping ``match_type='deterministic'`` — this
    is exactly the on-disk shape a $60-on-$100k variance produces under the default
    $50/1% thresholds.
    """
    return await create_test_recon_result(
        db,
        tenant_id,
        run_id,
        match_type="deterministic",
        variance_type="amount_mismatch",
        variance_amount=Decimal("60.00"),
        stripe_amount=Decimal("100000.00"),
        netsuite_amount=Decimal("99940.00"),
        bucket="needs_review",
    )


async def test_material_matched_row_surfaces_in_needs_review(client, db, finance_user):
    """A material matched row appears in needs_review — never in matches."""
    user, headers = finance_user
    await _enable_recon(db, user.tenant_id)
    run = await create_test_recon_run(db, user.tenant_id)
    # one ordinary (immaterial) deterministic match + one material matched row
    await create_test_recon_result(db, user.tenant_id, run.id, match_type="deterministic")
    material = await _seed_material_matched_row(db, user.tenant_id, run.id)
    await db.commit()

    # bucket summary: the material row lands in needs_review, not matches
    resp = await client.get(f"/api/v1/reconciliation/runs/{run.id}/buckets", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["matches"]["count"] == 1
    assert body["needs_review"]["count"] == 1

    # results filtered by needs_review include the material matched row by id
    resp = await client.get(
        f"/api/v1/reconciliation/runs/{run.id}/results?bucket=needs_review&limit=100",
        headers=headers,
    )
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()}
    assert str(material.id) in ids
    assert all(r["bucket"] == "needs_review" for r in resp.json())

    # and it is NOT present in the matches bucket
    resp = await client.get(
        f"/api/v1/reconciliation/runs/{run.id}/results?bucket=matches&limit=100",
        headers=headers,
    )
    assert resp.status_code == 200
    assert str(material.id) not in {r["id"] for r in resp.json()}


async def test_material_matched_row_not_bulk_approvable(client, db, finance_user):
    """needs_review is not bulk-approvable, so a material matched row can't be bulk-approved.

    Bulk-approving 'matches' must leave the material row (stored needs_review)
    un-flipped; bulk-approving 'needs_review' is rejected outright (400).
    """
    user, headers = finance_user
    run = await create_test_recon_run(db, user.tenant_id)
    await create_test_recon_result(db, user.tenant_id, run.id, match_type="deterministic")  # real match
    material = await _seed_material_matched_row(db, user.tenant_id, run.id)
    await db.commit()

    # bulk-approve 'matches' approves only the genuine match, never the material row
    resp = await client.post(
        f"/api/v1/reconciliation/runs/{run.id}/approve-bucket",
        json={"bucket": "matches"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["approved_count"] == 1

    refreshed = (
        await db.execute(select(ReconciliationResult.status).where(ReconciliationResult.id == material.id))
    ).scalar_one()
    assert refreshed == "suggested"  # material row untouched

    # bulk-approving the needs_review bucket itself is rejected
    resp = await client.post(
        f"/api/v1/reconciliation/runs/{run.id}/approve-bucket",
        json={"bucket": "needs_review"},
        headers=headers,
    )
    assert resp.status_code == 400


async def test_single_line_approve_records_notes_in_audit_payload(client, db, finance_user):
    """PATCH /results/{id}/approve persists request.notes in its audit event payload."""
    user, headers = finance_user
    run = await create_test_recon_run(db, user.tenant_id)
    res = await create_test_recon_result(db, user.tenant_id, run.id, match_type="deterministic", status="suggested")
    await db.commit()

    resp = await client.patch(
        f"/api/v1/reconciliation/results/{res.id}/approve",
        json={"result_id": str(res.id), "notes": "manual sign-off — fee rounding"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"

    event = (
        await db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "recon.approve",
                AuditEvent.resource_type == "reconciliation_result",
                AuditEvent.resource_id == str(res.id),
            )
        )
    ).scalar_one()
    assert event.payload is not None
    assert event.payload["notes"] == "manual sign-off — fee rounding"


async def test_single_line_approve_notes_none_recorded(client, db, finance_user):
    """When notes is omitted/None, the audit payload still carries notes=None (not dropped)."""
    user, headers = finance_user
    run = await create_test_recon_run(db, user.tenant_id)
    res = await create_test_recon_result(db, user.tenant_id, run.id, match_type="deterministic", status="suggested")
    await db.commit()

    resp = await client.patch(
        f"/api/v1/reconciliation/results/{res.id}/approve",
        json={"result_id": str(res.id), "notes": None},
        headers=headers,
    )
    assert resp.status_code == 200

    event = (
        await db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "recon.approve",
                AuditEvent.resource_id == str(res.id),
            )
        )
    ).scalar_one()
    assert event.payload == {"notes": None}


async def test_bulk_approve_skipped_count_mixed_bucket(client, db, finance_user):
    """skipped_count is exact when a bucket mixes approvable + already-approved + locked.

    Seed an auto_classifications bucket (deterministic + immaterial variance) with
    3 approvable (suggested), 2 already-approved, 1 locked. The single UPDATE flips
    the 3 suggested; skipped_count must equal the 3 pre-existing skip-status rows
    (2 approved + 1 locked) — and must NOT count the freshly approved rows even
    though they now also carry an 'approved' status.
    """
    user, headers = finance_user
    run = await create_test_recon_run(db, user.tenant_id)

    def _auto(**kw):
        # deterministic + immaterial variance ($0.12 on $10) → stored auto_classifications
        return create_test_recon_result(
            db,
            user.tenant_id,
            run.id,
            match_type="deterministic",
            variance_type="amount_mismatch",
            variance_amount=Decimal("0.12"),
            **kw,
        )

    for _ in range(3):
        await _auto(status="suggested")
    for _ in range(2):
        await _auto(status="approved")
    await _auto(status="locked")
    await db.commit()

    resp = await client.post(
        f"/api/v1/reconciliation/runs/{run.id}/approve-bucket",
        json={"bucket": "auto_classifications", "notes": "month-end"},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["approved_count"] == 3  # the 3 suggested rows
    assert body["skipped_count"] == 3  # 2 approved + 1 locked, NOT the 3 just-approved

    # every approvable row in the bucket is now approved (none left suggested)
    remaining_suggested = (
        await db.execute(
            select(func.count(ReconciliationResult.id)).where(
                ReconciliationResult.run_id == run.id,
                bucket_conditions("auto_classifications"),
                ReconciliationResult.status == "suggested",
            )
        )
    ).scalar_one()
    assert remaining_suggested == 0


async def test_bulk_approve_mixed_bucket_per_line_audit_invariants(client, db, finance_user):
    """HITL invariants hold on a mixed bucket: one per-line audit per *approved* row,
    one summary event, all sharing the batch correlation_id; skipped rows get none."""
    user, headers = finance_user
    run = await create_test_recon_run(db, user.tenant_id)

    def _auto(**kw):
        return create_test_recon_result(
            db,
            user.tenant_id,
            run.id,
            match_type="deterministic",
            variance_type="amount_mismatch",
            variance_amount=Decimal("0.12"),
            **kw,
        )

    await _auto(status="suggested")
    await _auto(status="suggested")
    await _auto(status="approved")  # already approved — must be skipped, no new audit
    await db.commit()

    resp = await client.post(
        f"/api/v1/reconciliation/runs/{run.id}/approve-bucket",
        json={"bucket": "auto_classifications"},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["approved_count"] == 2
    assert body["skipped_count"] == 1
    corr = body["correlation_id"]

    # exactly one per-line audit row per approved line, sharing the correlation_id
    per_line = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "recon.approve",
                    AuditEvent.correlation_id == corr,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(per_line) == 2
    assert {e.resource_type for e in per_line} == {"reconciliation_result"}

    # one summary event sharing the same correlation_id, recording the notes payload
    summary = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "recon.bulk_approve",
                    AuditEvent.correlation_id == corr,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(summary) == 1
    assert summary[0].resource_type == "reconciliation_run"
    assert summary[0].payload["bucket"] == "auto_classifications"
    assert summary[0].payload["approved_count"] == 2
    assert "notes" in summary[0].payload


async def test_bulk_approve_skipped_count_all_already_approved(client, db, finance_user):
    """A bucket with nothing approvable → approved_count 0, skipped_count = pre-existing skips."""
    user, headers = finance_user
    run = await create_test_recon_run(db, user.tenant_id)
    await create_test_recon_result(db, user.tenant_id, run.id, match_type="deterministic", status="approved")
    await create_test_recon_result(db, user.tenant_id, run.id, match_type="deterministic", status="locked")
    await db.commit()

    resp = await client.post(
        f"/api/v1/reconciliation/runs/{run.id}/approve-bucket",
        json={"bucket": "matches"},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["approved_count"] == 0
    assert body["skipped_count"] == 2
