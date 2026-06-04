"""Tests for OrderReconJob — order-level reconciliation job runner."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.models.canonical import PayoutLine
from app.models.tenant import TenantConfig
from app.schemas.order_reconciliation import (
    ChargeRecord,
    NSPaymentRecord,
    OrderMatchCandidate,
)
from app.services.reconciliation.order_recon_job import OrderReconJob
from tests.conftest import create_test_payout

TENANT_ID = str(uuid.uuid4())


def _make_payout_line_row(
    *,
    line_id: str = "pl-1",
    payout_id: str = "po-1",
    source_id: str = "ch_abc",
    line_type: str = "charge",
    amount: Decimal = Decimal("100.00"),
    fee: Decimal = Decimal("3.00"),
    net: Decimal = Decimal("97.00"),
    currency: str = "USD",
    description: str = "Framework Marketplace Order ID: R628489275-XU9EPZPD",
    arrival_date: date = date(2026, 3, 15),
) -> MagicMock:
    """Simulate a joined PayoutLine + Payout row from the DB query."""
    row = MagicMock()
    row.id = uuid.UUID(line_id) if "-" in line_id and len(line_id) > 10 else line_id
    row.source_id = source_id
    row.line_type = line_type
    row.amount = amount
    row.fee = fee
    row.net = net
    row.currency = currency
    row.description = description
    row.payout_id = payout_id
    # arrival_date comes from the joined Payout
    row.arrival_date = arrival_date
    return row


def _make_posting_row(
    *,
    posting_id: str = "np-1",
    netsuite_internal_id: str = "12345",
    record_type: str = "custdep",
    amount: Decimal = Decimal("100.00"),
    currency: str = "USD",
    transaction_date: date = date(2026, 3, 16),
    memo: str | None = None,
    related_payout_id: str | None = "R628489275",
    subsidiary_id: str | None = None,
) -> MagicMock:
    row = MagicMock()
    row.id = posting_id
    row.netsuite_internal_id = netsuite_internal_id
    row.record_type = record_type
    row.amount = amount
    row.currency = currency
    row.transaction_date = transaction_date
    row.memo = memo
    row.related_payout_id = related_payout_id
    row.subsidiary_id = subsidiary_id
    return row


def _mock_db() -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    # _store_results loads the tenant's materiality config; with no real DB the
    # query resolves to None so the runner falls back to the $50 / 1% defaults.
    no_config = MagicMock()
    no_config.scalar_one_or_none = MagicMock(return_value=None)
    db.execute = AsyncMock(return_value=no_config)
    return db


class TestFetchChargesFromPayoutLines:
    """Verifies query filters on line_type='charge' and date range via payout JOIN."""

    @pytest.mark.asyncio
    async def test_fetch_charges_filters_and_converts(self):
        """Charges are fetched from payout_lines with line_type='charge', date via payouts JOIN."""
        pl1 = _make_payout_line_row(
            source_id="ch_001",
            description="Framework Marketplace Order ID: R628489275-XU9EPZPD",
            arrival_date=date(2026, 3, 15),
        )
        pl2 = _make_payout_line_row(
            source_id="ch_002",
            description="STRIPE PAYOUT",
            amount=Decimal("50.00"),
            fee=Decimal("1.50"),
            net=Decimal("48.50"),
            arrival_date=date(2026, 3, 16),
        )

        db = _mock_db()
        # _fetch_charges now issues TWO queries in order:
        #   1. load_order_ref_pattern -> select(TenantConfig); scalar_one_or_none()
        #      returns None here so the runner falls back to the engine default
        #      R\d{9} pattern (mirrors a NULL-config / Framework tenant).
        #   2. the charges query -> result.all() yields (PayoutLine, arrival_date)
        #      tuples.
        # The mock must be query-order-aware: a single return value would feed the
        # MagicMock-typed pattern into re.compile and raise. Use a side_effect list.
        pattern_result = MagicMock()
        pattern_result.scalar_one_or_none = MagicMock(return_value=None)
        charges_result = MagicMock()
        charges_result.all.return_value = [
            (pl1, pl1.arrival_date),
            (pl2, pl2.arrival_date),
        ]
        db.execute = AsyncMock(side_effect=[pattern_result, charges_result])

        job = OrderReconJob(db=db, tenant_id=TENANT_ID)
        charges = await job._fetch_charges(
            date_from=date(2026, 3, 10),
            date_to=date(2026, 3, 20),
        )

        # Two queries executed: the order_ref_pattern load then the charges query.
        assert db.execute.call_count == 2

        assert len(charges) == 2

        # First charge: has order reference
        assert charges[0].source_id == "ch_001"
        assert charges[0].order_reference == "R628489275"
        assert charges[0].charge_date == date(2026, 3, 15)

        # Second charge: no order reference (STRIPE PAYOUT)
        assert charges[1].source_id == "ch_002"
        assert charges[1].order_reference is None
        assert charges[1].amount == Decimal("50.00")


class TestFetchDepositsWithOrderRef:
    """Verifies deposits fetched with order_reference from related_payout_id."""

    @pytest.mark.asyncio
    async def test_fetch_deposits_uses_related_payout_id_as_order_ref(self):
        np1 = _make_posting_row(
            netsuite_internal_id="12345",
            related_payout_id="R628489275",
            transaction_date=date(2026, 3, 16),
        )
        np2 = _make_posting_row(
            netsuite_internal_id="12346",
            related_payout_id=None,
            transaction_date=date(2026, 3, 17),
        )

        db = _mock_db()
        result_mock = MagicMock()
        result_mock.all.return_value = [np1, np2]
        execute_result = MagicMock()
        execute_result.scalars.return_value = result_mock
        db.execute = AsyncMock(return_value=execute_result)

        job = OrderReconJob(db=db, tenant_id=TENANT_ID)
        deposits = await job._fetch_deposits(
            date_from=date(2026, 3, 10),
            date_to=date(2026, 3, 20),
        )

        assert len(deposits) == 2

        # First deposit: order_reference from related_payout_id
        assert deposits[0].netsuite_internal_id == "12345"
        assert deposits[0].order_reference == "R628489275"

        # Second deposit: no order_reference
        assert deposits[1].netsuite_internal_id == "12346"
        assert deposits[1].order_reference is None


class TestRunProducesSummary:
    """Mock fetch + matching to verify ReconRunSummary with correct counts."""

    @pytest.mark.asyncio
    async def test_run_produces_summary_with_correct_counts(self):
        deposit_id = str(uuid.uuid4())
        charge = ChargeRecord(
            id="pl-1",
            source_id="ch_001",
            payout_line_id="pl-1",
            amount=Decimal("100.00"),
            fee=Decimal("3.00"),
            net=Decimal("97.00"),
            currency="USD",
            charge_date=date(2026, 3, 15),
            order_reference="R628489275",
        )
        deposit = NSPaymentRecord(
            id=deposit_id,
            netsuite_internal_id="12345",
            amount=Decimal("100.00"),
            currency="USD",
            transaction_date=date(2026, 3, 16),
            record_type="custdep",
            order_reference="R628489275",
        )
        matched_candidate = OrderMatchCandidate(
            charge=charge,
            deposit=deposit,
            match_type="deterministic",
            confidence=Decimal("1.0"),
            variance_amount=Decimal("0"),
            match_rule="order_reference_exact",
        )

        db = _mock_db()
        job = OrderReconJob(db=db, tenant_id=TENANT_ID)

        with (
            patch.object(job, "_fetch_charges", return_value=[charge]),
            patch.object(job, "_fetch_deposits", return_value=[deposit]),
            patch.object(job.engine, "match", return_value=[matched_candidate]),
        ):
            summary = await job.run(
                date_from=date(2026, 3, 10),
                date_to=date(2026, 3, 20),
            )

        assert summary.status == "completed"
        assert summary.total_payouts == 1  # total charges
        assert summary.total_deposits == 1
        assert summary.matched_count == 1
        assert summary.unmatched_count == 0
        assert summary.exception_count == 0
        assert summary.match_rate == Decimal("100.00")

    @pytest.mark.asyncio
    async def test_run_with_unmatched_charges(self):
        charge = ChargeRecord(
            id="pl-1",
            source_id="ch_001",
            payout_line_id="pl-1",
            amount=Decimal("100.00"),
            fee=Decimal("3.00"),
            net=Decimal("97.00"),
            currency="USD",
            charge_date=date(2026, 3, 15),
        )
        unmatched_candidate = OrderMatchCandidate(
            charge=charge,
            deposit=None,
            match_type="unmatched",
            confidence=Decimal("0"),
            variance_amount=Decimal("100.00"),
            variance_type="missing",
        )

        db = _mock_db()
        job = OrderReconJob(db=db, tenant_id=TENANT_ID)

        with (
            patch.object(job, "_fetch_charges", return_value=[charge]),
            patch.object(job, "_fetch_deposits", return_value=[]),
            patch.object(job.engine, "match", return_value=[unmatched_candidate]),
        ):
            summary = await job.run(
                date_from=date(2026, 3, 10),
                date_to=date(2026, 3, 20),
            )

        assert summary.matched_count == 0
        assert summary.unmatched_count == 1
        assert summary.match_rate == Decimal("0.00")


class TestStoresResultsWithNullPayoutId:
    """For order-level results, payout_id should be NULL, charge details in evidence JSON."""

    @pytest.mark.asyncio
    async def test_store_results_payout_id_null_evidence_has_charge_info(self):
        charge = ChargeRecord(
            id="pl-1",
            source_id="ch_001",
            payout_line_id="pl-1",
            amount=Decimal("100.00"),
            fee=Decimal("3.00"),
            net=Decimal("97.00"),
            currency="USD",
            charge_date=date(2026, 3, 15),
            order_reference="R628489275",
        )
        deposit_id = str(uuid.uuid4())
        deposit = NSPaymentRecord(
            id=deposit_id,
            netsuite_internal_id="12345",
            amount=Decimal("100.00"),
            currency="USD",
            transaction_date=date(2026, 3, 16),
            record_type="custdep",
            order_reference="R628489275",
        )
        candidate = OrderMatchCandidate(
            charge=charge,
            deposit=deposit,
            match_type="deterministic",
            confidence=Decimal("1.0"),
            variance_amount=Decimal("0"),
            match_rule="order_reference_exact",
        )

        db = _mock_db()
        job = OrderReconJob(db=db, tenant_id=TENANT_ID)
        run_id = uuid.uuid4()

        await job._store_results(run_id, [candidate])

        # Verify db.add was called with a ReconciliationResult
        assert db.add.called
        result = db.add.call_args_list[0][0][0]

        # payout_id must be NULL for order-level results
        assert result.payout_id is None

        # deposit_id must be set when matched
        assert result.deposit_id == uuid.UUID(deposit_id)

        # stripe_amount from charge.amount, netsuite_amount from deposit.amount
        assert result.stripe_amount == Decimal("100.00")
        assert result.netsuite_amount == Decimal("100.00")

        # evidence contains charge details
        assert result.evidence["charge_source_id"] == "ch_001"
        assert result.evidence["order_reference"] == "R628489275"
        assert result.evidence["charge_payout_line_id"] == "pl-1"

    @pytest.mark.asyncio
    async def test_store_results_unmatched_no_deposit_id(self):
        charge = ChargeRecord(
            id="pl-1",
            source_id="ch_002",
            payout_line_id="pl-1",
            amount=Decimal("50.00"),
            fee=Decimal("1.50"),
            net=Decimal("48.50"),
            currency="USD",
            charge_date=date(2026, 3, 15),
        )
        candidate = OrderMatchCandidate(
            charge=charge,
            deposit=None,
            match_type="unmatched",
            confidence=Decimal("0"),
            variance_amount=Decimal("50.00"),
            variance_type="missing",
        )

        db = _mock_db()
        job = OrderReconJob(db=db, tenant_id=TENANT_ID)
        run_id = uuid.uuid4()

        await job._store_results(run_id, [candidate])

        result = db.add.call_args_list[0][0][0]
        assert result.payout_id is None
        assert result.deposit_id is None
        assert result.match_type == "unmatched"
        assert result.evidence["charge_source_id"] == "ch_002"


# ---------------------------------------------------------------------------
# DB-backed: per-tenant order_ref_pattern threading through _fetch_charges
# (R3 Part 1, Task T3). These run against the local docker Postgres via the
# conftest ``db`` fixture (each test is rolled back). They assert that the
# extraction pattern loaded once in _fetch_charges comes from THIS tenant's
# TenantConfig.order_ref_pattern, and that a NULL pattern (Framework) extracts
# the R\d{9} ref byte-identically to the prior hardcoded behavior.
#
# Written rigorously following the recon DB-test patterns but NOT run in the
# implementer environment (no DB here); the PM runs them post-flight.
# ---------------------------------------------------------------------------


async def _seed_charge_line(
    db,
    tenant_id,
    *,
    description: str,
    source_id: str = "ch_db",
    amount: Decimal = Decimal("100.00"),
    fee: Decimal = Decimal("3.00"),
    net: Decimal = Decimal("97.00"),
    currency: str = "USD",
    arrival_date: date = date(2026, 3, 15),
    subsidiary_id: str | None = None,
) -> PayoutLine:
    """Seed a real Payout + PayoutLine(line_type='charge') for _fetch_charges.

    _fetch_charges JOINs payout_lines -> payouts for arrival_date, so the parent
    Payout must carry the arrival_date that lands inside the queried window
    (+/- _DATE_BUFFER).
    """
    payout = await create_test_payout(db, tenant_id, arrival_date=arrival_date)
    line = PayoutLine(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        dedupe_key=f"pl-{uuid.uuid4().hex}",
        source="stripe",
        source_id=source_id,
        subsidiary_id=subsidiary_id,
        payout_id=payout.id,
        line_type="charge",
        amount=amount,
        fee=fee,
        net=net,
        currency=currency,
        description=description,
    )
    db.add(line)
    await db.flush()
    return line


class TestFetchChargesUsesTenantPattern:
    """_fetch_charges threads THIS tenant's order_ref_pattern through extraction."""

    async def test_custom_pattern_tenant_extracts_via_that_pattern(self, db, tenant_a):
        """A tenant whose order_ref_pattern is set extracts using that pattern.

        The same description that yields nothing under the default R\\d{9} pattern
        yields the order-number under a custom ``(#\\d{4,})`` pattern.
        """
        cfg = (await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_a.id))).scalar_one()
        cfg.order_ref_pattern = r"(#\d{4,})"
        await db.flush()

        # No R\d{9} present — only a #-prefixed order number the custom pattern matches.
        await _seed_charge_line(
            db,
            tenant_a.id,
            source_id="ch_custom",
            description="Order #100423 settled",
        )

        job = OrderReconJob(db=db, tenant_id=str(tenant_a.id))
        charges = await job._fetch_charges(
            date_from=date(2026, 3, 10),
            date_to=date(2026, 3, 20),
        )

        assert len(charges) == 1
        assert charges[0].source_id == "ch_custom"
        # Extracted via the tenant's custom pattern, NOT the default R\d{9}.
        assert charges[0].order_reference == "#100423"

    async def test_null_pattern_tenant_extracts_r9_identically(self, db, tenant_a):
        """A NULL-pattern tenant (Framework) extracts R\\d{9} byte-identically.

        This is the #1 behavior-preserving invariant: NULL order_ref_pattern must
        produce the same order_reference as the prior hardcoded pattern.
        """
        # conftest leaves order_ref_pattern NULL — assert that precondition.
        cfg = (await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_a.id))).scalar_one()
        assert cfg.order_ref_pattern is None

        await _seed_charge_line(
            db,
            tenant_a.id,
            source_id="ch_fw",
            description="Framework Marketplace Order ID: R628489275-XU9EPZPD",
        )

        job = OrderReconJob(db=db, tenant_id=str(tenant_a.id))
        charges = await job._fetch_charges(
            date_from=date(2026, 3, 10),
            date_to=date(2026, 3, 20),
        )

        assert len(charges) == 1
        assert charges[0].source_id == "ch_fw"
        assert charges[0].order_reference == "R628489275"

    async def test_custom_pattern_differs_from_default_on_same_description(self, db, tenant_a, tenant_b):
        """Two tenants, same description, different patterns -> different refs.

        Proves the pattern is loaded per-tenant (not a process-global), so the
        custom-pattern tenant extracts differently than the default tenant on an
        identical payout-line description.
        """
        # tenant_a: custom pattern that captures a 10-digit run.
        cfg_a = (await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_a.id))).scalar_one()
        cfg_a.order_ref_pattern = r"(\d{10})"
        await db.flush()
        # tenant_b: default (NULL) pattern.

        # Description contains BOTH a 10-digit run and an R\d{9}.
        shared_description = "Order 1004230001 ref R628489275 settled"

        await _seed_charge_line(
            db,
            tenant_a.id,
            source_id="ch_a",
            description=shared_description,
        )
        await _seed_charge_line(
            db,
            tenant_b.id,
            source_id="ch_b",
            description=shared_description,
        )

        job_a = OrderReconJob(db=db, tenant_id=str(tenant_a.id))
        charges_a = await job_a._fetch_charges(date_from=date(2026, 3, 10), date_to=date(2026, 3, 20))

        job_b = OrderReconJob(db=db, tenant_id=str(tenant_b.id))
        charges_b = await job_b._fetch_charges(date_from=date(2026, 3, 10), date_to=date(2026, 3, 20))

        assert len(charges_a) == 1
        assert len(charges_b) == 1
        # Custom-pattern tenant -> 10-digit run; default tenant -> R\d{9}.
        assert charges_a[0].order_reference == "1004230001"
        assert charges_b[0].order_reference == "R628489275"
        assert charges_a[0].order_reference != charges_b[0].order_reference
