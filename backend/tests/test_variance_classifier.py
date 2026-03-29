"""Tests for variance classification."""

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.schemas.reconciliation import DepositRecord, PayoutRecord
from app.services.reconciliation.variance_classifier import classify_variance


def _payout(**kwargs) -> PayoutRecord:
    defaults = {
        "id": str(uuid.uuid4()),
        "source_id": "po_test",
        "amount": Decimal("1000.00"),
        "net_amount": Decimal("970.00"),
        "fee_amount": Decimal("30.00"),
        "currency": "USD",
        "arrival_date": date(2026, 3, 10),
    }
    defaults.update(kwargs)
    return PayoutRecord(**defaults)


def _deposit(**kwargs) -> DepositRecord:
    defaults = {
        "id": str(uuid.uuid4()),
        "netsuite_internal_id": "12001",
        "amount": Decimal("970.00"),
        "currency": "USD",
        "transaction_date": date(2026, 3, 10),
        "memo": None,
        "related_payout_id": None,
    }
    defaults.update(kwargs)
    return DepositRecord(**defaults)


class TestVarianceClassifier:
    def test_fees_variance(self):
        """Deposit matches gross amount (not net) — fee variance."""
        payout = _payout(amount=Decimal("1000.00"), net_amount=Decimal("970.00"), fee_amount=Decimal("30.00"))
        deposit = _deposit(amount=Decimal("1000.00"))

        vtype, explanation = classify_variance(
            payout=payout,
            deposit=deposit,
            amount_diff=Decimal("30.00"),
            day_diff=0,
            signals=["fee_variance"],
        )

        assert vtype == "fees"
        assert "fee" in explanation.lower()

    def test_fx_rounding_variance(self):
        """Small amount difference within rounding tolerance."""
        payout = _payout(net_amount=Decimal("970.00"))
        deposit = _deposit(amount=Decimal("969.97"))

        vtype, explanation = classify_variance(
            payout=payout,
            deposit=deposit,
            amount_diff=Decimal("0.03"),
            day_diff=0,
            signals=["amount_exact"],
        )

        assert vtype == "fx_rounding"

    def test_timing_variance(self):
        """Amount matches but dates differ — timing variance."""
        payout = _payout(net_amount=Decimal("970.00"), arrival_date=date(2026, 3, 10))
        deposit = _deposit(amount=Decimal("970.00"), transaction_date=date(2026, 3, 13))

        vtype, explanation = classify_variance(
            payout=payout,
            deposit=deposit,
            amount_diff=Decimal("0.00"),
            day_diff=3,
            signals=["amount_exact", "within_3_days"],
        )

        assert vtype == "timing"
        assert "3" in explanation

    def test_chargeback_variance(self):
        """Large negative variance consistent with chargeback amount."""
        payout = _payout(net_amount=Decimal("1940.00"), fee_amount=Decimal("60.00"))
        deposit = _deposit(amount=Decimal("1790.00"))

        vtype, explanation = classify_variance(
            payout=payout,
            deposit=deposit,
            amount_diff=Decimal("150.00"),
            day_diff=0,
            signals=["amount_within_fx_tolerance"],
        )

        assert vtype in ("chargeback", "manual_adjustment")

    def test_missing_variance_no_deposit(self):
        """Payout with no deposit at all."""
        payout = _payout(net_amount=Decimal("500.00"))

        vtype, explanation = classify_variance(
            payout=payout,
            deposit=None,
            amount_diff=Decimal("500.00"),
            day_diff=0,
            signals=[],
        )

        assert vtype == "missing"

    def test_no_variance(self):
        """Exact match, no variance."""
        payout = _payout(net_amount=Decimal("970.00"))
        deposit = _deposit(amount=Decimal("970.00"))

        vtype, explanation = classify_variance(
            payout=payout,
            deposit=deposit,
            amount_diff=Decimal("0.00"),
            day_diff=0,
            signals=["amount_exact", "same_day"],
        )

        assert vtype is None
