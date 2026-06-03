"""DB-backed write-time bucketing + run rollup tests for OrderReconJob (R2a T3).

These run against the local docker Postgres via the conftest ``db`` fixture (each
test is rolled back). They assert that ``_store_results`` persists the
four-bucket classification on each ``ReconciliationResult.bucket`` (computed via
``classify()`` with the tenant's materiality thresholds) and that ``run`` carries
the 4 per-bucket rollup counts alongside the legacy matched/exception/unmatched
counts.

For the order runner the materiality ``matched_amount`` base is
``candidate.charge.amount`` (the gross charge amount, which is also stored as
``stripe_amount``) — matching the migration backfill's relative base.

Written rigorously following the existing recon DB-test patterns but NOT run in
the implementer environment (no DB here); the PM runs them post-flight.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.models.reconciliation import ReconciliationResult, ReconciliationRun
from app.schemas.order_reconciliation import (
    ChargeRecord,
    NSPaymentRecord,
    OrderMatchCandidate,
)
from app.services.reconciliation.four_bucket_classifier import (
    BUCKET_AUTO_CLASSIFICATIONS,
    BUCKET_MATCHES,
    BUCKET_NEEDS_REVIEW,
    BUCKET_RULES,
)
from app.services.reconciliation.order_recon_job import OrderReconJob
from tests.conftest import create_test_netsuite_posting, create_test_recon_run

# Default materiality thresholds (TenantConfig defaults: $50 / 0.0100).
_MAT_ABS = Decimal("50")
_MAT_PCT = Decimal("0.0100")


def _charge(
    *,
    source_id: str = "ch_001",
    amount: Decimal = Decimal("100.00"),
    order_reference: str | None = "R628489275",
) -> ChargeRecord:
    return ChargeRecord(
        id="pl-1",
        source_id=source_id,
        payout_line_id="pl-1",
        amount=amount,
        fee=Decimal("3.00"),
        net=amount - Decimal("3.00"),
        currency="USD",
        charge_date=date(2026, 3, 15),
        order_reference=order_reference,
    )


def _deposit(*, amount: Decimal = Decimal("100.00")) -> NSPaymentRecord:
    return NSPaymentRecord(
        id=str(uuid.uuid4()),
        netsuite_internal_id="12345",
        amount=amount,
        currency="USD",
        transaction_date=date(2026, 3, 16),
        record_type="custdep",
        order_reference="R628489275",
    )


def _candidate(
    *,
    charge: ChargeRecord,
    deposit: NSPaymentRecord | None,
    match_type: str,
    confidence: Decimal = Decimal("1.0"),
    variance_amount: Decimal = Decimal("0"),
    variance_type: str | None = None,
) -> OrderMatchCandidate:
    return OrderMatchCandidate(
        charge=charge,
        deposit=deposit,
        match_type=match_type,
        confidence=confidence,
        variance_amount=variance_amount,
        variance_type=variance_type,
        match_rule="order_reference_exact",
    )


async def _seed_deposit_postings(db, tenant_id, candidates) -> None:
    """Seed a netsuite_postings parent row for every matched candidate's deposit.

    The order runner writes ``deposit_id = uuid.UUID(candidate.deposit.id)`` (FK to
    netsuite_postings); payout_id is always NULL. Seed only the deposit parents so
    the reconciliation_results_deposit_id_fkey is satisfied.
    """
    for candidate in candidates:
        if candidate.deposit is None:
            continue
        await create_test_netsuite_posting(
            db,
            tenant_id,
            id=uuid.UUID(candidate.deposit.id),
            netsuite_internal_id=candidate.deposit.netsuite_internal_id,
            record_type=candidate.deposit.record_type,
            transaction_date=candidate.deposit.transaction_date,
            amount=candidate.deposit.amount,
            currency=candidate.deposit.currency,
        )


async def _stored_bucket_by_source(db, run_id) -> dict[str, str]:
    rows = (
        await db.execute(
            select(ReconciliationResult.evidence, ReconciliationResult.bucket).where(
                ReconciliationResult.run_id == run_id
            )
        )
    ).all()
    return {ev["charge_source_id"]: bucket for ev, bucket in rows}


async def test_store_results_persists_bucket_per_row(db, tenant_a):
    """Each stored result carries the classify()-computed bucket under default materiality."""
    run = await create_test_recon_run(db, tenant_a.id)

    candidates = [
        # deterministic + no variance -> matches
        _candidate(
            charge=_charge(source_id="ch_match"),
            deposit=_deposit(),
            match_type="deterministic",
        ),
        # deterministic + immaterial variance -> auto_classifications
        _candidate(
            charge=_charge(source_id="ch_auto", amount=Decimal("1000.00")),
            deposit=_deposit(amount=Decimal("999.50")),
            match_type="deterministic",
            variance_amount=Decimal("0.50"),
            variance_type="amount_mismatch",
        ),
        # fuzzy + immaterial variance -> rules
        _candidate(
            charge=_charge(source_id="ch_rule", amount=Decimal("100.00")),
            deposit=_deposit(amount=Decimal("99.60")),
            match_type="fuzzy",
            confidence=Decimal("0.85"),
            variance_amount=Decimal("0.40"),
            variance_type="amount_mismatch",
        ),
        # deterministic + material-by-abs variance (>$50) -> needs_review
        _candidate(
            charge=_charge(source_id="ch_needs_abs", amount=Decimal("100000.00")),
            deposit=_deposit(amount=Decimal("99940.00")),
            match_type="deterministic",
            variance_amount=Decimal("60.00"),
            variance_type="amount_mismatch",
        ),
        # fuzzy + material-by-pct variance (>1% of charge) -> needs_review
        _candidate(
            charge=_charge(source_id="ch_needs_pct", amount=Decimal("100.00")),
            deposit=_deposit(amount=Decimal("97.00")),
            match_type="fuzzy",
            confidence=Decimal("0.80"),
            variance_amount=Decimal("3.00"),
            variance_type="amount_mismatch",
        ),
        # unmatched -> needs_review
        _candidate(
            charge=_charge(source_id="ch_unmatched", amount=Decimal("50.00")),
            deposit=None,
            match_type="unmatched",
            confidence=Decimal("0"),
            variance_amount=Decimal("50.00"),
            variance_type="missing",
        ),
    ]

    await _seed_deposit_postings(db, tenant_a.id, candidates)
    job = OrderReconJob(db=db, tenant_id=str(tenant_a.id))
    await job._store_results(run.id, candidates)

    by_source = await _stored_bucket_by_source(db, run.id)
    assert by_source["ch_match"] == BUCKET_MATCHES
    assert by_source["ch_auto"] == BUCKET_AUTO_CLASSIFICATIONS
    assert by_source["ch_rule"] == BUCKET_RULES
    assert by_source["ch_needs_abs"] == BUCKET_NEEDS_REVIEW
    assert by_source["ch_needs_pct"] == BUCKET_NEEDS_REVIEW
    assert by_source["ch_unmatched"] == BUCKET_NEEDS_REVIEW


async def test_material_matched_row_lands_needs_review(db, tenant_a):
    """A matched (deterministic) row with material variance is routed to needs_review."""
    run = await create_test_recon_run(db, tenant_a.id)
    candidate = _candidate(
        charge=_charge(source_id="ch_big_var", amount=Decimal("10000.00")),
        deposit=_deposit(amount=Decimal("9900.00")),
        match_type="deterministic",
        variance_amount=Decimal("100.00"),  # > $50 abs
        variance_type="amount_mismatch",
    )
    await _seed_deposit_postings(db, tenant_a.id, [candidate])
    job = OrderReconJob(db=db, tenant_id=str(tenant_a.id))
    await job._store_results(run.id, [candidate])

    stored = (await db.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run.id))).scalar_one()
    assert stored.bucket == BUCKET_NEEDS_REVIEW


async def test_store_results_uses_default_materiality_when_no_config(db, tenant_a):
    """When the tenant has no TenantConfig row, defaults ($50 / 1%) apply.

    ``create_test_tenant`` already seeds a config, so we delete it to exercise the
    fall-back path; an immaterial variance must NOT be routed to needs_review.
    """
    from app.models.tenant import TenantConfig

    await db.execute(TenantConfig.__table__.delete().where(TenantConfig.tenant_id == tenant_a.id))
    await db.flush()

    run = await create_test_recon_run(db, tenant_a.id)
    candidate = _candidate(
        charge=_charge(source_id="ch_small", amount=Decimal("1000.00")),
        deposit=_deposit(amount=Decimal("999.50")),
        match_type="deterministic",
        variance_amount=Decimal("0.50"),  # immaterial under $50 / 1%
        variance_type="amount_mismatch",
    )
    await _seed_deposit_postings(db, tenant_a.id, [candidate])
    job = OrderReconJob(db=db, tenant_id=str(tenant_a.id))
    await job._store_results(run.id, [candidate])

    stored = (await db.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run.id))).scalar_one()
    assert stored.bucket == BUCKET_AUTO_CLASSIFICATIONS


async def test_run_persists_rollup_counts(db, tenant_a):
    """A full run() persists the 4 per-bucket rollup counts alongside legacy counts."""
    charges = [
        _charge(source_id="ch_match"),
        _charge(source_id="ch_needs_abs", amount=Decimal("100000.00")),
        _charge(source_id="ch_unmatched", amount=Decimal("50.00")),
    ]
    deposits = [_deposit(), _deposit(amount=Decimal("99940.00"))]
    candidates = [
        _candidate(charge=charges[0], deposit=deposits[0], match_type="deterministic"),
        _candidate(
            charge=charges[1],
            deposit=deposits[1],
            match_type="deterministic",
            variance_amount=Decimal("60.00"),
            variance_type="amount_mismatch",
        ),
        _candidate(
            charge=charges[2],
            deposit=None,
            match_type="unmatched",
            confidence=Decimal("0"),
            variance_amount=Decimal("50.00"),
            variance_type="missing",
        ),
    ]

    await _seed_deposit_postings(db, tenant_a.id, candidates)
    job = OrderReconJob(db=db, tenant_id=str(tenant_a.id))

    from unittest.mock import patch

    with (
        patch.object(job, "_fetch_charges", return_value=charges),
        patch.object(job, "_fetch_deposits", return_value=deposits),
        patch.object(job.engine, "match", return_value=candidates),
    ):
        summary = await job.run(date_from=date(2026, 3, 1), date_to=date(2026, 3, 31))

    run = (
        await db.execute(select(ReconciliationRun).where(ReconciliationRun.id == uuid.UUID(summary.run_id)))
    ).scalar_one()

    # New per-bucket rollup counts.
    assert run.matches_count == 1
    assert run.auto_classifications_count == 0
    assert run.rules_count == 0
    assert run.needs_review_count == 2  # material matched + unmatched

    # Legacy counts left intact.
    assert run.matched_count == 2  # deterministic + deterministic (both "matched")
    assert run.exception_count == 0
    assert run.unmatched_count == 1
