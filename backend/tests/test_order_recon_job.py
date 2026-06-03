"""Tests for OrderReconJob — order-level reconciliation job runner."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.order_reconciliation import (
    ChargeRecord,
    NSPaymentRecord,
    OrderMatchCandidate,
)
from app.services.reconciliation.order_recon_job import OrderReconJob

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
        # _fetch_charges uses result.all() returning (PayoutLine, arrival_date) tuples
        execute_result = MagicMock()
        execute_result.all.return_value = [
            (pl1, pl1.arrival_date),
            (pl2, pl2.arrival_date),
        ]
        db.execute = AsyncMock(return_value=execute_result)

        job = OrderReconJob(db=db, tenant_id=TENANT_ID)
        charges = await job._fetch_charges(
            date_from=date(2026, 3, 10),
            date_to=date(2026, 3, 20),
        )

        # Verify the query was executed
        db.execute.assert_called_once()

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
