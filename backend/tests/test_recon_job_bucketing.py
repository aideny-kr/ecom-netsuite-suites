"""DB-backed write-time bucketing + run rollup tests for ReconJobRunner (R2a T3).

The payout-level runner counterpart of ``test_order_recon_job_bucketing.py``.
These run against the local docker Postgres via the conftest ``db`` fixture (each
test is rolled back). They assert that ``_store_results`` persists the
four-bucket classification on each ``ReconciliationResult.bucket`` (computed via
``classify()`` with the tenant's materiality thresholds) and that ``run`` carries
the 4 per-bucket rollup counts alongside the legacy matched/exception/unmatched
counts.

For the payout runner the materiality ``matched_amount`` base is
``candidate.payout.net_amount`` — the same value already persisted as
``stripe_amount`` and used by the matching engine as the variance base, matching
the migration backfill's relative base.

Written rigorously following the existing recon DB-test patterns but NOT run in
the implementer environment (no DB here); the PM runs them post-flight.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import select

from app.models.reconciliation import ReconciliationResult, ReconciliationRun
from app.schemas.reconciliation import DepositRecord, MatchCandidate, PayoutRecord
from app.services.reconciliation.four_bucket_classifier import (
    BUCKET_AUTO_CLASSIFICATIONS,
    BUCKET_MATCHES,
    BUCKET_NEEDS_REVIEW,
    BUCKET_RULES,
)
from app.services.reconciliation.recon_job import ReconJobRunner
from tests.conftest import (
    create_test_netsuite_posting,
    create_test_payout,
    create_test_recon_run,
)


def _payout(
    *,
    source_id: str = "po_001",
    net_amount: Decimal = Decimal("970.00"),
) -> PayoutRecord:
    return PayoutRecord(
        id=str(uuid.uuid4()),
        source_id=source_id,
        amount=net_amount + Decimal("30.00"),
        net_amount=net_amount,
        fee_amount=Decimal("30.00"),
        currency="USD",
        arrival_date=date(2026, 3, 10),
    )


def _deposit(*, amount: Decimal = Decimal("970.00")) -> DepositRecord:
    return DepositRecord(
        id=str(uuid.uuid4()),
        netsuite_internal_id="20001",
        amount=amount,
        currency="USD",
        transaction_date=date(2026, 3, 10),
        memo="Stripe payout",
        related_payout_id="po_001",
    )


def _candidate(
    *,
    payout: PayoutRecord,
    deposits: list[DepositRecord],
    match_type: str,
    confidence: Decimal = Decimal("1.0"),
    variance_amount: Decimal = Decimal("0"),
    variance_type: str | None = None,
) -> MatchCandidate:
    return MatchCandidate(
        payout=payout,
        deposits=deposits,
        match_type=match_type,
        confidence=confidence,
        variance_amount=variance_amount,
        variance_type=variance_type,
        match_rule="payout_id_exact",
    )


async def _seed_parents(db, tenant_id, candidates) -> None:
    """Seed payouts + netsuite_postings parents for each candidate's FKs.

    The payout runner writes both ``payout_id = uuid.UUID(candidate.payout.id)``
    (FK to payouts) and ``deposit_id = uuid.UUID(candidate.deposits[0].id)`` (FK to
    netsuite_postings). Seed both so the result-row FK constraints are satisfied.
    """
    for candidate in candidates:
        await create_test_payout(
            db,
            tenant_id,
            id=uuid.UUID(candidate.payout.id),
            source_id=candidate.payout.source_id,
            amount=candidate.payout.amount,
            fee_amount=candidate.payout.fee_amount,
            net_amount=candidate.payout.net_amount,
            currency=candidate.payout.currency,
            arrival_date=candidate.payout.arrival_date,
        )
        for deposit in candidate.deposits:
            await create_test_netsuite_posting(
                db,
                tenant_id,
                id=uuid.UUID(deposit.id),
                netsuite_internal_id=deposit.netsuite_internal_id,
                record_type="custdep",
                transaction_date=deposit.transaction_date,
                amount=deposit.amount,
                currency=deposit.currency,
                related_payout_id=deposit.related_payout_id,
            )


async def _stored_bucket_by_source(db, run_id) -> dict[str, str]:
    rows = (
        await db.execute(
            select(ReconciliationResult.evidence, ReconciliationResult.bucket).where(
                ReconciliationResult.run_id == run_id
            )
        )
    ).all()
    return {ev["payout_source_id"]: bucket for ev, bucket in rows}


async def test_store_results_persists_bucket_per_row(db, tenant_a):
    """Each stored payout result carries the classify()-computed bucket (default materiality)."""
    run = await create_test_recon_run(db, tenant_a.id)

    candidates = [
        # deterministic + no variance -> matches
        _candidate(
            payout=_payout(source_id="po_match"),
            deposits=[_deposit()],
            match_type="deterministic",
        ),
        # deterministic + immaterial variance -> auto_classifications
        _candidate(
            payout=_payout(source_id="po_auto", net_amount=Decimal("1000.00")),
            deposits=[_deposit(amount=Decimal("999.50"))],
            match_type="deterministic",
            variance_amount=Decimal("0.50"),
            variance_type="fees",
        ),
        # fuzzy + immaterial variance -> rules
        _candidate(
            payout=_payout(source_id="po_rule", net_amount=Decimal("100.00")),
            deposits=[_deposit(amount=Decimal("99.60"))],
            match_type="fuzzy",
            confidence=Decimal("0.85"),
            variance_amount=Decimal("0.40"),
            variance_type="fees",
        ),
        # deterministic + material-by-abs variance (>$50) -> needs_review
        _candidate(
            payout=_payout(source_id="po_needs_abs", net_amount=Decimal("100000.00")),
            deposits=[_deposit(amount=Decimal("99940.00"))],
            match_type="deterministic",
            variance_amount=Decimal("60.00"),
            variance_type="fees",
        ),
        # fuzzy + material-by-pct variance (>1% of net_amount) -> needs_review
        _candidate(
            payout=_payout(source_id="po_needs_pct", net_amount=Decimal("100.00")),
            deposits=[_deposit(amount=Decimal("97.00"))],
            match_type="fuzzy",
            confidence=Decimal("0.80"),
            variance_amount=Decimal("3.00"),
            variance_type="fees",
        ),
        # unmatched -> needs_review
        _candidate(
            payout=_payout(source_id="po_unmatched", net_amount=Decimal("50.00")),
            deposits=[],
            match_type="unmatched",
            confidence=Decimal("0"),
            variance_amount=Decimal("50.00"),
            variance_type="missing",
        ),
    ]

    await _seed_parents(db, tenant_a.id, candidates)
    runner = ReconJobRunner(db=db, tenant_id=str(tenant_a.id))
    await runner._store_results(run.id, candidates)

    by_source = await _stored_bucket_by_source(db, run.id)
    assert by_source["po_match"] == BUCKET_MATCHES
    assert by_source["po_auto"] == BUCKET_AUTO_CLASSIFICATIONS
    assert by_source["po_rule"] == BUCKET_RULES
    assert by_source["po_needs_abs"] == BUCKET_NEEDS_REVIEW
    assert by_source["po_needs_pct"] == BUCKET_NEEDS_REVIEW
    assert by_source["po_unmatched"] == BUCKET_NEEDS_REVIEW


async def test_material_matched_row_lands_needs_review(db, tenant_a):
    """A matched (deterministic) payout row with material variance routes to needs_review."""
    run = await create_test_recon_run(db, tenant_a.id)
    candidate = _candidate(
        payout=_payout(source_id="po_big_var", net_amount=Decimal("10000.00")),
        deposits=[_deposit(amount=Decimal("9900.00"))],
        match_type="deterministic",
        variance_amount=Decimal("100.00"),  # > $50 abs
        variance_type="fees",
    )
    await _seed_parents(db, tenant_a.id, [candidate])
    runner = ReconJobRunner(db=db, tenant_id=str(tenant_a.id))
    await runner._store_results(run.id, [candidate])

    stored = (await db.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run.id))).scalar_one()
    assert stored.bucket == BUCKET_NEEDS_REVIEW


async def test_store_results_uses_default_materiality_when_no_config(db, tenant_a):
    """When the tenant has no TenantConfig row, defaults ($50 / 1%) apply."""
    from app.models.tenant import TenantConfig

    await db.execute(TenantConfig.__table__.delete().where(TenantConfig.tenant_id == tenant_a.id))
    await db.flush()

    run = await create_test_recon_run(db, tenant_a.id)
    candidate = _candidate(
        payout=_payout(source_id="po_small", net_amount=Decimal("1000.00")),
        deposits=[_deposit(amount=Decimal("999.50"))],
        match_type="deterministic",
        variance_amount=Decimal("0.50"),  # immaterial under $50 / 1%
        variance_type="fees",
    )
    await _seed_parents(db, tenant_a.id, [candidate])
    runner = ReconJobRunner(db=db, tenant_id=str(tenant_a.id))
    await runner._store_results(run.id, [candidate])

    stored = (await db.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run.id))).scalar_one()
    assert stored.bucket == BUCKET_AUTO_CLASSIFICATIONS


async def test_run_persists_rollup_counts(db, tenant_a):
    """A full run() persists the 4 per-bucket rollup counts alongside legacy counts."""
    payouts = [
        _payout(source_id="po_match"),
        _payout(source_id="po_needs_abs", net_amount=Decimal("100000.00")),
        _payout(source_id="po_unmatched", net_amount=Decimal("50.00")),
    ]
    deposits = [_deposit(), _deposit(amount=Decimal("99940.00"))]
    candidates = [
        _candidate(payout=payouts[0], deposits=[deposits[0]], match_type="deterministic"),
        _candidate(
            payout=payouts[1],
            deposits=[deposits[1]],
            match_type="deterministic",
            variance_amount=Decimal("60.00"),
            variance_type="fees",
        ),
        _candidate(
            payout=payouts[2],
            deposits=[],
            match_type="unmatched",
            confidence=Decimal("0"),
            variance_amount=Decimal("50.00"),
            variance_type="missing",
        ),
    ]

    await _seed_parents(db, tenant_a.id, candidates)
    runner = ReconJobRunner(db=db, tenant_id=str(tenant_a.id))

    with (
        patch.object(runner, "_fetch_payouts", return_value=payouts),
        patch.object(runner, "_fetch_deposits", return_value=deposits),
        patch.object(runner.engine, "match", return_value=candidates),
    ):
        summary = await runner.run(date_from=date(2026, 3, 1), date_to=date(2026, 3, 31))

    run = (
        await db.execute(select(ReconciliationRun).where(ReconciliationRun.id == uuid.UUID(summary.run_id)))
    ).scalar_one()

    # New per-bucket rollup counts.
    assert run.matches_count == 1
    assert run.auto_classifications_count == 0
    assert run.rules_count == 0
    assert run.needs_review_count == 2  # material matched + unmatched

    # Legacy counts left intact.
    assert run.matched_count == 2
    assert run.exception_count == 0
    assert run.unmatched_count == 1
