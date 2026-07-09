"""R3-A: period-scoped close readiness.

The FE CloseChecklist gates the Lock Period button, but POST /close/{period}
closes EVERY completed run whose date range falls inside the month — so the
readiness gate must be computed over the SAME period scope, not the selected
run. ``close_scope.py`` is the single source of truth for both predicates
(run selection + left-for-review), consumed by ``close_period`` AND
``GET /reconciliation/close-readiness/{period}``.

Every count keys on the authoritative ``status``/``bucket`` only, never the
advisory confidence composite (the R2 decoupling pattern).

DB-backed tests use the conftest ``db`` fixture (local docker Postgres) —
if unreachable in the implementer sandbox, the PM runs them post-flight.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import and_

from app.schemas.reconciliation import ReconBucketSummary, ReconCloseReadiness
from app.services.reconciliation.close_scope import (
    closeable_runs_conditions,
    left_for_review_conditions,
    parse_period,
)
from tests.conftest import create_test_recon_result, create_test_recon_run

API = "/api/v1/reconciliation"


async def _enable_recon(db, tenant_id):
    """Enable the reconciliation feature flag (defaults off) for HTTP tests.

    Mirrors tests/test_recon_bucket_reviewer.py::_enable_recon (test-factory
    consolidation is an already-logged follow-up).
    """
    from app.services.feature_flag_service import clear_cache, set_flag

    clear_cache()
    await set_flag(db, tenant_id, "reconciliation", True)
    await db.flush()
    clear_cache()


# ---------------------------------------------------------------------------
# close_scope helpers — sandbox-runnable (no DB)
# ---------------------------------------------------------------------------


def test_parse_period_returns_month_bounds():
    assert parse_period("2026-04") == (date(2026, 4, 1), date(2026, 4, 30))
    # Leap-year February
    assert parse_period("2024-02") == (date(2024, 2, 1), date(2024, 2, 29))


@pytest.mark.parametrize("bad", ["garbage", "2026", "2026-13", "2026-00", "2026-04-01", "20-26-04"])
def test_parse_period_rejects_bad_format(bad):
    with pytest.raises(ValueError):
        parse_period(bad)


def test_closeable_runs_conditions_rejects_bad_period():
    with pytest.raises(ValueError):
        closeable_runs_conditions(uuid.uuid4(), "not-a-period")


def test_closeable_runs_conditions_compiled_sql():
    """The helper encodes EXACTLY close_period's run selection: tenant +
    date_from >= first day + date_to <= last day + status='completed'."""
    tenant_id = uuid.uuid4()
    sql = str(and_(*closeable_runs_conditions(tenant_id, "2026-04")).compile(compile_kwargs={"literal_binds": True}))
    assert "tenant_id" in sql
    assert "date_from >= '2026-04-01'" in sql
    assert "date_to <= '2026-04-30'" in sql
    assert "status = 'completed'" in sql


def test_left_for_review_conditions_compiled_sql():
    """status='auto_matched' AND bucket='needs_review' — the rows close
    deliberately leaves unlocked (HITL)."""
    sql = str(and_(*left_for_review_conditions()).compile(compile_kwargs={"literal_binds": True}))
    assert "status = 'auto_matched'" in sql
    assert "bucket = 'needs_review'" in sql


# ---------------------------------------------------------------------------
# Schemas — close_readiness moved OFF the per-run bucket summary onto the
# period endpoint (the single authoritative readiness source).
# ---------------------------------------------------------------------------


def test_bucket_summary_has_no_close_readiness_field():
    assert "close_readiness" not in ReconBucketSummary.model_fields


def test_close_readiness_schema_shape():
    r = ReconCloseReadiness(
        period="2026-04",
        runs_in_scope=2,
        in_scope_run_ids=["a", "b"],
        open_exceptions=1,
        suggested=0,
        left_for_review=3,
        carried_forward=4,
    )
    assert r.model_dump() == {
        "period": "2026-04",
        "runs_in_scope": 2,
        "in_scope_run_ids": ["a", "b"],
        "open_exceptions": 1,
        "suggested": 0,
        "left_for_review": 3,
        "carried_forward": 4,
    }


# ---------------------------------------------------------------------------
# GET /reconciliation/close-readiness/{period} — DB-backed
# ---------------------------------------------------------------------------


async def test_close_readiness_counts(client, db, finance_user):
    """Each count keys on the authoritative status/bucket only.

    - open_exceptions: status='pending' AND match_type != 'unmatched'
    - suggested:       status='suggested'
    - left_for_review: status='auto_matched' AND bucket='needs_review' — mirrors
      close_period()'s left-for-review predicate (rows close leaves unlocked).
    """
    user, headers = finance_user
    await _enable_recon(db, user.tenant_id)
    run = await create_test_recon_run(db, user.tenant_id)  # 2026-04-20..24 → period 2026-04
    # 1 open exception: pending on a MATCHED line
    await create_test_recon_result(db, user.tenant_id, run.id, match_type="fuzzy", status="pending")
    # NOT an open exception: pending + unmatched (an expected needs_review row)
    await create_test_recon_result(
        db, user.tenant_id, run.id, match_type="unmatched", variance_type="missing", status="pending"
    )
    # 1 suggested
    await create_test_recon_result(db, user.tenant_id, run.id, match_type="deterministic", status="suggested")
    # 1 left for review: auto_matched + stored needs_review (material matched row)
    await create_test_recon_result(
        db,
        user.tenant_id,
        run.id,
        match_type="deterministic",
        status="auto_matched",
        variance_type="amount_mismatch",
        variance_amount=Decimal("60.00"),
        stripe_amount=Decimal("100000.00"),
        netsuite_amount=Decimal("99940.00"),
        bucket="needs_review",
    )
    # NOT left for review: auto_matched in the matches bucket
    await create_test_recon_result(db, user.tenant_id, run.id, match_type="deterministic", status="auto_matched")
    # NOT counted anywhere: an approved (dispositioned) needs_review row
    await create_test_recon_result(
        db, user.tenant_id, run.id, match_type="unmatched", variance_type="missing", status="approved"
    )
    await db.commit()

    resp = await client.get(f"{API}/close-readiness/2026-04", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {
        "period": "2026-04",
        "runs_in_scope": 1,
        "in_scope_run_ids": [str(run.id)],
        "open_exceptions": 1,
        "suggested": 1,
        "left_for_review": 1,
        "carried_forward": 0,
    }


async def test_close_readiness_aggregates_over_all_runs_in_period(client, db, finance_user):
    """THE R3-A bug: a clean selected run must not report 'ready' while another
    completed run in the same month still has suggested/material/pending rows —
    POST /close/{period} will close BOTH."""
    user, headers = finance_user
    await _enable_recon(db, user.tenant_id)
    # Run A: clean — everything approved or auto_matched outside needs_review.
    run_a = await create_test_recon_run(db, user.tenant_id)
    await create_test_recon_result(db, user.tenant_id, run_a.id, match_type="deterministic", status="approved")
    await create_test_recon_result(db, user.tenant_id, run_a.id, match_type="deterministic", status="auto_matched")
    # Run B: same month, unready — 1 suggested + 1 material left-for-review +
    # 1 pending on a matched line.
    run_b = await create_test_recon_run(db, user.tenant_id)
    await create_test_recon_result(db, user.tenant_id, run_b.id, match_type="deterministic", status="suggested")
    await create_test_recon_result(
        db,
        user.tenant_id,
        run_b.id,
        match_type="deterministic",
        status="auto_matched",
        variance_type="amount_mismatch",
        variance_amount=Decimal("60.00"),
        bucket="needs_review",
    )
    await create_test_recon_result(db, user.tenant_id, run_b.id, match_type="fuzzy", status="pending")
    await db.commit()

    resp = await client.get(f"{API}/close-readiness/2026-04", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {
        "period": "2026-04",
        "runs_in_scope": 2,
        # Sorted for determinism — the FE only does a membership check.
        "in_scope_run_ids": sorted([str(run_a.id), str(run_b.id)]),
        "open_exceptions": 1,
        "suggested": 1,
        "left_for_review": 1,
        "carried_forward": 0,
    }


async def test_close_readiness_tenant_scoped(client, db, finance_user, tenant_b):
    """Another tenant's rows must not leak into any count.

    Cross-tenant rows are seeded on tenant A's OWN run: the run_id IS in scope,
    so only the results tenant_id predicate can exclude them — that is what
    actually proves tenant scoping (same discriminating-seed pattern as the
    former buckets-endpoint test; this regression class is live in this repo —
    commit 34b8f50 fixed exactly a missing tenant filter in the
    evidence-download query). The foreign run additionally proves the RUNS
    query is tenant-scoped (runs_in_scope must stay 1).
    """
    user, headers = finance_user
    await _enable_recon(db, user.tenant_id)
    run = await create_test_recon_run(db, user.tenant_id)
    # Foreign tenant's own run in the SAME period, with a left-for-review row —
    # excluded from runs_in_scope and all counts by tenant scoping.
    foreign_run = await create_test_recon_run(db, tenant_b.id)
    await create_test_recon_result(
        db,
        tenant_b.id,
        foreign_run.id,
        match_type="deterministic",
        status="auto_matched",
        variance_type="amount_mismatch",
        variance_amount=Decimal("60.00"),
        bucket="needs_review",
    )
    # Cross-tenant rows on tenant A's OWN run — one per count.
    # → open_exceptions (pending on a matched line)
    await create_test_recon_result(db, tenant_b.id, run.id, match_type="fuzzy", status="pending")
    # → suggested
    await create_test_recon_result(db, tenant_b.id, run.id, match_type="deterministic", status="suggested")
    # → left_for_review (auto_matched + stored needs_review)
    await create_test_recon_result(
        db,
        tenant_b.id,
        run.id,
        match_type="deterministic",
        status="auto_matched",
        variance_type="amount_mismatch",
        variance_amount=Decimal("60.00"),
        bucket="needs_review",
    )
    await db.commit()

    resp = await client.get(f"{API}/close-readiness/2026-04", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {
        "period": "2026-04",
        "runs_in_scope": 1,
        "in_scope_run_ids": [str(run.id)],
        "open_exceptions": 0,
        "suggested": 0,
        "left_for_review": 0,
        "carried_forward": 0,
    }


async def test_close_readiness_excludes_out_of_scope_runs(client, db, finance_user):
    """Runs close_period would NOT close are invisible to readiness:
    non-completed runs in the month, completed runs outside the month, and
    completed runs SPANNING the month boundary (R4-A: a month-spanning run is
    out of its own derived period's scope — its id must not appear in
    in_scope_run_ids, or the FE run_in_scope gate would pass vacuously)."""
    user, headers = finance_user
    await _enable_recon(db, user.tenant_id)
    # In-period but still running — close_period only selects status='completed'.
    running = await create_test_recon_run(db, user.tenant_id, status="running")
    await create_test_recon_result(db, user.tenant_id, running.id, match_type="deterministic", status="suggested")
    # Completed but in a DIFFERENT month.
    other_month = await create_test_recon_run(db, user.tenant_id)
    other_month.date_from = date(2026, 5, 2)
    other_month.date_to = date(2026, 5, 6)
    await create_test_recon_result(db, user.tenant_id, other_month.id, match_type="deterministic", status="suggested")
    # Completed but SPANNING the April/May boundary — close_period('2026-04')
    # requires date_to <= last day of April, so this run is NOT closeable.
    spanning = await create_test_recon_run(db, user.tenant_id)
    spanning.date_from = date(2026, 4, 25)
    spanning.date_to = date(2026, 5, 3)
    await create_test_recon_result(db, user.tenant_id, spanning.id, match_type="deterministic", status="suggested")
    # One in-scope run with a single suggested row.
    in_scope = await create_test_recon_run(db, user.tenant_id)
    await create_test_recon_result(db, user.tenant_id, in_scope.id, match_type="deterministic", status="suggested")
    await db.commit()

    resp = await client.get(f"{API}/close-readiness/2026-04", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {
        "period": "2026-04",
        "runs_in_scope": 1,
        "in_scope_run_ids": [str(in_scope.id)],
        "open_exceptions": 0,
        "suggested": 1,
        "left_for_review": 0,
        "carried_forward": 0,
    }


async def test_close_readiness_zero_runs_all_zeros(client, db, finance_user):
    """No in-scope runs → 200 with all-zero counts (NOT close_period's 404 —
    the gate is a read, an empty month is simply 'nothing to close')."""
    user, headers = finance_user
    await _enable_recon(db, user.tenant_id)

    resp = await client.get(f"{API}/close-readiness/2026-01", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {
        "period": "2026-01",
        "runs_in_scope": 0,
        "in_scope_run_ids": [],
        "open_exceptions": 0,
        "suggested": 0,
        "left_for_review": 0,
        "carried_forward": 0,
    }


@pytest.mark.parametrize("bad", ["garbage", "2026-13", "2026-04-01"])
async def test_close_readiness_invalid_period_400(client, db, finance_user, bad):
    """Period validated exactly as close_period does."""
    user, headers = finance_user
    await _enable_recon(db, user.tenant_id)

    resp = await client.get(f"{API}/close-readiness/{bad}", headers=headers)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Period must be YYYY-MM format"
