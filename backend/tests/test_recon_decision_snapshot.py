"""Eligibility is snapshotted when a row is DECIDED — by every path, not just reject.

Design decision 2026-08-07 (Option A, cumulative): a decision made inside a period
still counts toward the matcher's track record after that period closes. The metric
answers "is the matcher trustworthy enough to post unattended", which is a property
of the matcher over time, not of an accounting period.

That forces the denominator off `status` and onto the snapshot column, because
`close_period` rewrites approved -> locked underneath it. The previous version keyed
on status and had two defects that pushed the number in opposite directions:

  · it admitted every `approved` row believing approve enforced the envelope. It does
    not — approve checks only run-open and non-terminal — so a fuzzy $500-variance row
    counted. True 1/1 reported as 0.1, biased toward "safe to post".
  · a close deleted every approval from the denominator while rejections persisted:
    0.05 -> 1.0 with no change to the matcher.

Both are reproduced below as tests, so the fix cannot silently regress.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.api.v1.reconciliation import close_period, get_close_readiness  # noqa: F401
from app.services.reconciliation.recon_reject import reject_result
from tests.conftest import create_test_recon_result, create_test_recon_run, create_test_user

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def user_a(db, tenant_a):
    u, _ = await create_test_user(db, tenant_a)
    await db.flush()
    return u


async def _eligible_row(db, tenant_id, run, **kw):
    """A row the autonomy envelope would admit: deterministic, matched, zero variance,
    known amount."""
    opts = dict(
        status="auto_matched",
        bucket="matches",
        match_type="deterministic",
        variance_amount=Decimal("0"),
        stripe_amount=Decimal("100.00"),
        netsuite_amount=Decimal("100.00"),
    )
    opts.update(kw)
    r = await create_test_recon_result(db, tenant_id, run.id, **opts)
    await db.flush()
    return r


async def test_approving_an_eligible_row_records_the_snapshot(db, tenant_a, user_a):
    """Approve must snapshot eligibility exactly as reject does — otherwise the
    denominator has to infer it from `status`, which another workflow rewrites."""
    from app.services.reconciliation.recon_decision import record_decision_snapshot

    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    r = await _eligible_row(db, tenant_a.id, run)

    record_decision_snapshot(r)
    assert r.envelope_eligible_at_decision is True
    # An approval is never a false positive — the human agreed with the matcher.
    assert r.counts_as_false_positive is not True


async def test_approving_a_row_the_envelope_would_refuse_is_not_in_the_denominator(db, tenant_a, user_a):
    """The first blocker. Approve imposes NO envelope restriction, so a fuzzy row with
    a material variance is freely approvable — and used to land in the denominator on
    the false premise that "approved implies eligible"."""
    from app.services.reconciliation.recon_decision import record_decision_snapshot

    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    r = await _eligible_row(db, tenant_a.id, run, match_type="fuzzy", variance_amount=Decimal("500"))

    record_decision_snapshot(r)
    assert r.envelope_eligible_at_decision is False


async def test_the_rate_is_unchanged_across_a_close(db, tenant_a, user_a):
    """The second blocker, and the reason Option A was chosen.

    close_period rewrites approved -> locked while rejected rows keep their status
    forever. Keyed on status, a month-end close deleted every approval from the
    denominator and the rate went 0.05 -> 1.0 with no change to the matcher. Keyed on
    the snapshot, closing the books is invisible to the metric.
    """
    from app.services.reconciliation.recon_decision import (
        envelope_false_positive_rate,
        record_decision_snapshot,
    )

    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    rows = [await _eligible_row(db, tenant_a.id, run) for _ in range(20)]

    await reject_result(db, result=rows[0], user=user_a, reason="wrong_match", note=None, run=run)
    for r in rows[1:]:
        record_decision_snapshot(r)
        r.status = "approved"
    await db.flush()

    before = await envelope_false_positive_rate(db, tenant_id=tenant_a.id, run_id=run.id)
    assert before["decided"] == 20
    assert before["false_positives"] == 1
    assert before["rate"] == pytest.approx(0.05)

    # Close the books. Every approved row becomes 'locked'.
    for r in rows[1:]:
        r.status = "locked"
    await db.flush()

    after = await envelope_false_positive_rate(db, tenant_id=tenant_a.id, run_id=run.id)
    assert after == before, "closing a period must not move the matcher's track record"


async def test_rate_is_none_with_no_decisions(db, tenant_a):
    """A confident 0.0 on an empty corpus is exactly how an unsafe autonomy decision
    gets justified. 'No evidence' must not render as 'no errors'."""
    from app.services.reconciliation.recon_decision import envelope_false_positive_rate

    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await db.flush()

    out = await envelope_false_positive_rate(db, tenant_id=tenant_a.id, run_id=run.id)
    assert out["decided"] == 0
    assert out["rate"] is None


async def test_undecided_rows_are_not_in_the_denominator(db, tenant_a, user_a):
    """Only rows a human actually ruled on carry information. An eligible row nobody
    touched is not evidence either way."""
    from app.services.reconciliation.recon_decision import envelope_false_positive_rate

    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await _eligible_row(db, tenant_a.id, run)  # never decided
    await db.flush()

    out = await envelope_false_positive_rate(db, tenant_id=tenant_a.id, run_id=run.id)
    assert out["decided"] == 0


async def test_python_and_sql_ladders_agree(db, tenant_a):
    """The drift guard, and the reason this file exists as much as the rate does.

    Three decide paths are SET-BASED UPDATEs that never load an ORM object, so the
    ladder needs a SQL form as well as a Python one. Two representations of one rule
    is precisely how they diverge — silently, since each is individually tested. This
    asserts they agree on a matrix that crosses every rung.
    """
    from sqlalchemy import select

    from app.models.reconciliation import ReconciliationResult
    from app.services.reconciliation.recon_decision import (
        eligible_sql,
        is_envelope_eligible,
    )

    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    matrix = [
        {},  # the eligible baseline
        {"match_type": "fuzzy"},
        {"bucket": "needs_review"},
        {"variance_amount": Decimal("0.01")},
        {"variance_amount": Decimal("-5")},
        {"stripe_amount": None},
        {"status": "approved"},
        {"status": "rejected"},
        {"match_type": "unmatched", "bucket": "unmatched"},
    ]
    made = [await _eligible_row(db, tenant_a.id, run, **kw) for kw in matrix]
    await db.flush()

    sql_ids = set(
        (await db.execute(select(ReconciliationResult.id).where(ReconciliationResult.run_id == run.id, eligible_sql())))
        .scalars()
        .all()
    )
    for row, kw in zip(made, matrix):
        py = is_envelope_eligible(row)
        sql = row.id in sql_ids
        assert py == sql, f"ladder disagreement for {kw or 'baseline'}: python={py} sql={sql}"


async def test_bulk_bucket_approve_snapshots_each_row(db, tenant_a, user_a):
    """The set-based path must record the snapshot too, and record it PER ROW.

    This is the half most likely to be wrong: there is no ORM object, so eligibility
    is a SQL CASE rather than the Python helper, and a mistake here is invisible to
    every test that only exercises the helper. The bucket deliberately mixes an
    eligible row with one the envelope refuses, so a CASE that collapsed to a
    constant would fail.
    """
    from app.api.v1.reconciliation import approve_bucket
    from app.schemas.reconciliation import ReconBucketApprove

    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    good = await _eligible_row(db, tenant_a.id, run)
    bad = await _eligible_row(db, tenant_a.id, run, stripe_amount=None)  # amount unknown
    await db.flush()

    await approve_bucket(str(run.id), ReconBucketApprove(bucket="matches"), user=user_a, db=db)
    await db.refresh(good)
    await db.refresh(bad)

    assert good.status == "approved"
    assert good.envelope_eligible_at_decision is True
    assert bad.envelope_eligible_at_decision is False, (
        "an amount-unknown row is not envelope-eligible; a constant CASE would miss this"
    )
    assert good.counts_as_false_positive is not True


async def test_bulk_approve_lands_in_the_denominator(db, tenant_a, user_a):
    """End to end through the real endpoint: bulk-approved rows are decided, so they
    count — otherwise the rate is computed over rejects alone and reads ~1.0."""
    from app.api.v1.reconciliation import approve_bucket
    from app.schemas.reconciliation import ReconBucketApprove
    from app.services.reconciliation.recon_decision import envelope_false_positive_rate

    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    rows = [await _eligible_row(db, tenant_a.id, run) for _ in range(4)]
    await reject_result(db, result=rows[0], user=user_a, reason="wrong_match", note=None, run=run)
    await db.flush()

    await approve_bucket(str(run.id), ReconBucketApprove(bucket="matches"), user=user_a, db=db)
    await db.flush()

    out = await envelope_false_positive_rate(db, tenant_id=tenant_a.id, run_id=run.id)
    assert out["decided"] == 4, "1 rejected + 3 bulk-approved"
    assert out["false_positives"] == 1
    assert out["rate"] == pytest.approx(0.25)
