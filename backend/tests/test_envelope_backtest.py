"""The back-test must count REAL contradictions and refuse to imply correctness."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.reconciliation.envelope_backtest import envelope_self_contradiction
from tests.conftest import (
    create_test_netsuite_posting,
    create_test_recon_result,
    create_test_recon_run,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def deposit_a(db, tenant_a):
    d = await create_test_netsuite_posting(db, tenant_a.id, netsuite_internal_id="A1")
    await db.flush()
    return d


@pytest.fixture
async def deposit_b(db, tenant_a):
    d = await create_test_netsuite_posting(db, tenant_a.id, netsuite_internal_id="B1")
    await db.flush()
    return d


async def _run_over(db, tenant_id, *, df=date(2026, 4, 1), dt=date(2026, 4, 30)):
    """A run pinned to an explicit window — the back-test groups by window, so two
    runs only compare when their windows match exactly."""
    run = await create_test_recon_run(db, tenant_id, status="completed")
    run.date_from, run.date_to = df, dt
    await db.flush()
    return run


async def _row(db, tenant_id, run, oref, deposit_id, *, graded=True):
    r = await create_test_recon_result(
        db,
        tenant_id,
        run.id,
        status="auto_matched",
        bucket="matches" if graded else "needs_review",
        match_type="deterministic" if graded else "fuzzy",
        variance_amount=Decimal("0") if graded else Decimal("5"),
        stripe_amount=Decimal("100.00"),
        evidence={"order_reference": oref},
        deposit_id=deposit_id,
    )
    await db.flush()
    return r


async def test_two_envelope_grade_matches_to_different_deposits_is_a_contradiction(db, tenant_a, deposit_a, deposit_b):
    """The load-bearing case. Both runs claimed the match was safe to post
    unattended, and they named different deposits. At least one was wrong."""
    r1 = await _run_over(db, tenant_a.id)
    r2 = await _run_over(db, tenant_a.id)
    await _row(db, tenant_a.id, r1, "FW-1", deposit_a.id)
    await _row(db, tenant_a.id, r2, "FW-1", deposit_b.id)

    out = await envelope_self_contradiction(db, tenant_id=tenant_a.id)
    assert out["contradictions"] == 1
    assert out["rate"] == pytest.approx(1.0)
    assert out["upper_bound_95_if_zero"] is None, "a bound is only reported for zero events"


async def test_disagreement_with_an_ungraded_side_is_not_a_contradiction(db, tenant_a, deposit_a, deposit_b):
    """The case that makes the metric honest rather than alarmist.

    A later run seeing a NetSuite deposit that had not synced yet will legitimately
    reach a different answer — but it does NOT claim envelope grade for it. That is
    the envelope declining to be certain, which is correct behaviour, not an error.
    On production data this is the whole of the 1,377 observed disagreements.
    """
    r1 = await _run_over(db, tenant_a.id)
    r2 = await _run_over(db, tenant_a.id)
    await _row(db, tenant_a.id, r1, "FW-2", deposit_a.id, graded=True)
    await _row(db, tenant_a.id, r2, "FW-2", deposit_b.id, graded=False)

    out = await envelope_self_contradiction(db, tenant_id=tenant_a.id)
    assert out["deposit_disagreements_any_grade"] == 1, "the disagreement is still visible"
    assert out["contradictions"] == 0, "but it is not charged against the envelope"


async def test_zero_contradictions_reports_a_bound_not_a_zero_rate(db, tenant_a, deposit_a):
    """Reporting 0.0 invites 'the matcher is correct'. The rule-of-three bound says
    what a clean run actually licenses, which is much weaker."""
    r1 = await _run_over(db, tenant_a.id)
    r2 = await _run_over(db, tenant_a.id)
    await _row(db, tenant_a.id, r1, "FW-3", deposit_a.id)
    await _row(db, tenant_a.id, r2, "FW-3", deposit_a.id)  # same deposit — agrees

    out = await envelope_self_contradiction(db, tenant_id=tenant_a.id)
    assert out["contradictions"] == 0
    # Rule of three is a large-n approximation and its output is a PROBABILITY.
    # Unclamped at n=1 it returned 3.0 — "a 300% upper bound" — from the one module
    # whose entire premise is that no caller may over-read the number. Below the
    # floor it now says nothing, which is the honest answer.
    assert out["upper_bound_95_if_zero"] is None, "n=1 is far below the rule-of-three floor"
    assert out["is_lower_bound"] is True
    assert "not correctness" in out["not_a_correctness_measure"]


async def test_empty_population_reports_none_never_zero(db, tenant_a):
    """'Nothing to measure' must not render as 'no errors found' — the exact way an
    unsafe autonomy decision gets justified."""
    out = await envelope_self_contradiction(db, tenant_id=tenant_a.id)
    assert out["rerun_envelope_grade_orders"] == 0
    assert out["rate"] is None
    assert out["upper_bound_95_if_zero"] is None


async def test_single_run_orders_are_not_counted(db, tenant_a, deposit_a):
    """An order reconciled once cannot contradict itself; it carries no information
    and must not dilute the denominator."""
    r1 = await _run_over(db, tenant_a.id)
    await _row(db, tenant_a.id, r1, "FW-4", deposit_a.id)

    out = await envelope_self_contradiction(db, tenant_id=tenant_a.id)
    assert out["rerun_envelope_grade_orders"] == 0


async def test_is_tenant_scoped(db, tenant_a, tenant_b):
    """Another tenant's contradictions must never appear in this tenant's number."""
    da = await create_test_netsuite_posting(db, tenant_b.id, netsuite_internal_id="TB-A")
    dbp = await create_test_netsuite_posting(db, tenant_b.id, netsuite_internal_id="TB-B")
    await db.flush()
    r1 = await _run_over(db, tenant_b.id)
    r2 = await _run_over(db, tenant_b.id)
    await _row(db, tenant_b.id, r1, "FW-5", da.id)
    await _row(db, tenant_b.id, r2, "FW-5", dbp.id)

    out = await envelope_self_contradiction(db, tenant_id=tenant_a.id)
    assert out["contradictions"] == 0
    other = await envelope_self_contradiction(db, tenant_id=tenant_b.id)
    assert other["contradictions"] == 1


async def test_windows_must_match_to_be_compared(db, tenant_a, deposit_a, deposit_b):
    """Two runs over DIFFERENT windows are not re-runs of the same question, so a
    different answer is not a contradiction."""
    r1 = await _run_over(db, tenant_a.id, df=date(2026, 4, 1), dt=date(2026, 4, 30))
    r2 = await _run_over(db, tenant_a.id, df=date(2026, 5, 1), dt=date(2026, 5, 31))
    await _row(db, tenant_a.id, r1, "FW-6", deposit_a.id)
    await _row(db, tenant_a.id, r2, "FW-6", deposit_b.id)

    out = await envelope_self_contradiction(db, tenant_id=tenant_a.id)
    assert out["contradictions"] == 0


async def test_a_split_order_in_one_run_is_not_a_contradiction(db, tenant_a, deposit_a, deposit_b):
    """The blocker this file failed to catch the first time.

    A split/partial-capture order legitimately produces 2 charges + 2 deposits in a
    SINGLE run — equal counts, full confidence, explicitly not ambiguous
    (`_match_same_ref_group` step 1). The first version counted distinct
    envelope-grade deposits across the whole (window, order) group, so one CORRECT
    run satisfied "more than one graded deposit" by itself and was charged with a
    contradiction it never made. Comparing per-run SETS is the fix: both runs answer
    {A, B}, which is agreement.
    """
    r1 = await _run_over(db, tenant_a.id)
    r2 = await _run_over(db, tenant_a.id)
    for run in (r1, r2):
        await _row(db, tenant_a.id, run, "FW-SPLIT", deposit_a.id)
        await _row(db, tenant_a.id, run, "FW-SPLIT", deposit_b.id)

    out = await envelope_self_contradiction(db, tenant_id=tenant_a.id)
    assert out["contradictions"] == 0, "identical split answers are agreement, not conflict"


async def test_runs_scoped_to_different_subsidiaries_are_not_compared(db, tenant_a, deposit_a, deposit_b):
    """A subsidiary-scoped run sees a different deposit population by design
    (_fetch_deposits filters on subsidiary_id), so a different answer is the CORRECT
    answer to a different question — not a contradiction."""
    r1 = await _run_over(db, tenant_a.id)
    r2 = await _run_over(db, tenant_a.id)
    r1.subsidiary_id, r2.subsidiary_id = "1", "2"
    await db.flush()
    await _row(db, tenant_a.id, r1, "FW-SUB", deposit_a.id)
    await _row(db, tenant_a.id, r2, "FW-SUB", deposit_b.id)

    out = await envelope_self_contradiction(db, tenant_id=tenant_a.id)
    assert out["contradictions"] == 0


async def test_results_from_a_failed_run_are_not_an_envelope_claim(db, tenant_a, deposit_a, deposit_b):
    """_store_results commits BEFORE run.status is set, and the error path then writes
    status='failed' — so a crashed run leaves rows behind. Partial output is not a
    claim that the envelope stood behind those matches."""
    r1 = await _run_over(db, tenant_a.id)
    r2 = await _run_over(db, tenant_a.id)
    r2.status = "failed"
    await db.flush()
    await _row(db, tenant_a.id, r1, "FW-FAIL", deposit_a.id)
    await _row(db, tenant_a.id, r2, "FW-FAIL", deposit_b.id)

    out = await envelope_self_contradiction(db, tenant_id=tenant_a.id)
    assert out["contradictions"] == 0


# --- the scheduled path: computed off-request, read from storage ----------------


async def test_task_is_registered_and_scheduled():
    """A task absent from conf.include never boots; absent from beat_schedule it
    never fires. Both failures are silent — the number simply stays stale forever,
    which reads as 'stable' rather than 'stopped'."""
    from app.workers.celery_app import celery_app

    assert "app.workers.tasks.envelope_backtest_task" in celery_app.conf.include
    entry = celery_app.conf.beat_schedule.get("envelope-backtest")
    assert entry is not None, "scheduled or it never runs"
    assert entry["task"] == "tasks.envelope_backtest"


async def test_compute_skips_tenants_with_no_recon_data(db, tenant_a):
    """An empty population must not be recorded as a clean zero. 'contradictions: 0'
    for a tenant that never reconciled anything is a confident nothing."""
    from app.workers.tasks.envelope_backtest_task import compute_for_all_tenants

    out = await compute_for_all_tenants(db)
    assert str(tenant_a.id) not in out["per_tenant"]
    assert out["tenants_with_contradictions"] == 0


async def test_compute_surfaces_a_contradicting_tenant(db, tenant_a, deposit_a, deposit_b):
    """The count the ops digest can alarm on without parsing the payload."""
    from app.workers.tasks.envelope_backtest_task import compute_for_all_tenants

    r1 = await _run_over(db, tenant_a.id)
    r2 = await _run_over(db, tenant_a.id)
    await _row(db, tenant_a.id, r1, "FW-T", deposit_a.id)
    await _row(db, tenant_a.id, r2, "FW-T", deposit_b.id)

    out = await compute_for_all_tenants(db)
    assert out["tenants_with_contradictions"] == 1
    assert out["per_tenant"][str(tenant_a.id)]["contradictions"] == 1


async def test_data_status_reads_the_stored_result_and_its_age(db, tenant_a, finance_user):
    """The endpoint must NOT recompute — the query is a ~5s full scan and this
    endpoint is on the recon UI's load path. It reads the last completed job, and
    carries `computed_at` so a bound that stopped refreshing cannot pass as current.
    """
    from datetime import datetime, timezone

    from app.api.v1.reconciliation import get_data_status
    from app.models.job import Job

    user, _ = finance_user
    done = datetime.now(timezone.utc)
    db.add(
        Job(
            tenant_id=user.tenant_id,
            job_type="envelope_backtest",
            status="completed",
            completed_at=done,
            result_summary={
                "tenants_measured": 1,
                "tenants_with_contradictions": 0,
                "per_tenant": {str(user.tenant_id): {"contradictions": 0, "rate": 0.0}},
            },
        )
    )
    await db.flush()

    out = await get_data_status(user=user, db=db)
    assert out["envelope_backtest"]["available"] is True
    assert out["envelope_backtest"]["contradictions"] == 0
    assert out["envelope_backtest"]["computed_at"] == done


async def test_data_status_says_not_computed_rather_than_zero(db, finance_user):
    """With no job yet the answer is 'not computed', never a zero. Absence of a
    measurement must not render as absence of errors."""
    from app.api.v1.reconciliation import get_data_status

    user, _ = finance_user
    out = await get_data_status(user=user, db=db)
    assert out["envelope_backtest"]["available"] is False
    assert "contradictions" not in out["envelope_backtest"]
