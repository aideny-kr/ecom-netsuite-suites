"""Integration tests for reconciliation job runner."""

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.schemas.reconciliation import DepositRecord, PayoutRecord
from app.services.reconciliation.recon_job import ReconJobRunner


@pytest.fixture
def mock_db():
    """Mock async database session."""
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.fixture
def sample_payouts() -> list[PayoutRecord]:
    return [
        PayoutRecord(
            id=str(uuid.uuid4()),
            source_id="po_job01",
            amount=Decimal("1000.00"),
            net_amount=Decimal("970.00"),
            fee_amount=Decimal("30.00"),
            currency="USD",
            arrival_date=date(2026, 3, 10),
        ),
        PayoutRecord(
            id=str(uuid.uuid4()),
            source_id="po_job02",
            amount=Decimal("500.00"),
            net_amount=Decimal("485.00"),
            fee_amount=Decimal("15.00"),
            currency="USD",
            arrival_date=date(2026, 3, 11),
        ),
    ]


@pytest.fixture
def sample_deposits() -> list[DepositRecord]:
    return [
        DepositRecord(
            id=str(uuid.uuid4()),
            netsuite_internal_id="20001",
            amount=Decimal("970.00"),
            currency="USD",
            transaction_date=date(2026, 3, 10),
            memo="Stripe payout po_job01",
            related_payout_id="po_job01",
        ),
    ]


class TestReconJobRunner:
    @pytest.mark.asyncio
    async def test_run_produces_summary(self, mock_db, sample_payouts, sample_deposits):
        """Job runner should return a summary with match counts."""
        runner = ReconJobRunner(db=mock_db, tenant_id=str(uuid.uuid4()))

        with (
            patch.object(runner, "_fetch_payouts", return_value=sample_payouts),
            patch.object(runner, "_fetch_deposits", return_value=sample_deposits),
            patch.object(runner, "_store_results", return_value=None),
        ):
            summary = await runner.run(
                date_from=date(2026, 3, 1),
                date_to=date(2026, 3, 31),
            )

        assert summary.total_payouts == 2
        assert summary.total_deposits == 1
        assert summary.matched_count >= 1
        assert summary.status == "completed"

    @pytest.mark.asyncio
    async def test_run_stores_results(self, mock_db, sample_payouts, sample_deposits):
        """Job runner should call _store_results with match candidates."""
        runner = ReconJobRunner(db=mock_db, tenant_id=str(uuid.uuid4()))

        stored_results = []

        async def capture_store(run_id, candidates):
            stored_results.extend(candidates)

        with (
            patch.object(runner, "_fetch_payouts", return_value=sample_payouts),
            patch.object(runner, "_fetch_deposits", return_value=sample_deposits),
            patch.object(runner, "_store_results", side_effect=capture_store),
        ):
            await runner.run(date_from=date(2026, 3, 1), date_to=date(2026, 3, 31))

        # Should have at least 1 matched + 1 unmatched
        assert len(stored_results) >= 2

    @pytest.mark.asyncio
    async def test_run_with_subsidiary_filter(self, mock_db):
        """Job runner should pass subsidiary_id to fetch methods."""
        runner = ReconJobRunner(db=mock_db, tenant_id=str(uuid.uuid4()))

        with (
            patch.object(runner, "_fetch_payouts", return_value=[]) as mock_fetch_p,
            patch.object(runner, "_fetch_deposits", return_value=[]),
            patch.object(runner, "_store_results", return_value=None),
        ):
            await runner.run(
                date_from=date(2026, 3, 1),
                date_to=date(2026, 3, 31),
                subsidiary_id="sub_123",
            )

        mock_fetch_p.assert_called_once()
        call_kwargs = mock_fetch_p.call_args
        assert call_kwargs[1].get("subsidiary_id") == "sub_123" or "sub_123" in str(call_kwargs)
