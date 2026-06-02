import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

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
