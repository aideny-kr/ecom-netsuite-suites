"""Tests for order-level fuzzy matching."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.schemas.order_reconciliation import ChargeRecord, NSPaymentRecord
from app.services.reconciliation.order_fuzzy_matcher import fuzzy_match


# --- Test helpers ---
def _make_charge(
    id: str = "c1",
    source_id: str = "ch_test",
    amount: Decimal = Decimal("100.00"),
    fee: Decimal = Decimal("3.20"),
    currency: str = "USD",
    charge_date: date = date(2026, 3, 1),
    description: str | None = None,
    order_reference: str | None = None,
) -> ChargeRecord:
    return ChargeRecord(
        id=id,
        source_id=source_id,
        payout_line_id=f"pl_{id}",
        amount=amount,
        fee=fee,
        net=amount - fee,
        currency=currency,
        charge_date=charge_date,
        description=description,
        order_reference=order_reference,
    )


def _make_deposit(
    id: str = "d1",
    netsuite_internal_id: str = "12345",
    amount: Decimal = Decimal("100.00"),
    currency: str = "USD",
    transaction_date: date = date(2026, 3, 1),
    record_type: str = "custdep",
    memo: str | None = None,
    order_reference: str | None = None,
) -> NSPaymentRecord:
    return NSPaymentRecord(
        id=id,
        netsuite_internal_id=netsuite_internal_id,
        amount=amount,
        currency=currency,
        transaction_date=transaction_date,
        record_type=record_type,
        memo=memo,
        order_reference=order_reference,
    )


class TestFuzzyMatching:
    def test_exact_amount_close_date(self):
        """Same amount, 1 day apart, same currency -> fuzzy match, confidence >= 0.60."""
        charges = [_make_charge(amount=Decimal("149.99"), charge_date=date(2026, 3, 1))]
        deposits = [_make_deposit(amount=Decimal("149.99"), transaction_date=date(2026, 3, 2))]
        results = fuzzy_match(charges, deposits)
        assert len(results) == 1
        assert results[0].match_type == "fuzzy"
        assert results[0].confidence >= Decimal("0.60")
        # Lists should be empty after matching
        assert len(charges) == 0
        assert len(deposits) == 0

    def test_amount_too_different(self):
        """$100 vs $200 -> unmatched (>2% and >$50 difference)."""
        charges = [_make_charge(amount=Decimal("100.00"))]
        deposits = [_make_deposit(amount=Decimal("200.00"))]
        results = fuzzy_match(charges, deposits)
        assert len(results) == 0
        # Both should remain unmatched
        assert len(charges) == 1
        assert len(deposits) == 1

    def test_date_too_far(self):
        """Same amount, 20 days apart -> unmatched."""
        charges = [_make_charge(amount=Decimal("100.00"), charge_date=date(2026, 3, 1))]
        deposits = [_make_deposit(amount=Decimal("100.00"), transaction_date=date(2026, 3, 21))]
        results = fuzzy_match(charges, deposits)
        assert len(results) == 0
        assert len(charges) == 1
        assert len(deposits) == 1

    def test_currency_mismatch(self):
        """Same amount, USD vs EUR -> unmatched."""
        charges = [_make_charge(amount=Decimal("100.00"), currency="USD")]
        deposits = [_make_deposit(amount=Decimal("100.00"), currency="EUR")]
        results = fuzzy_match(charges, deposits)
        assert len(results) == 0
        assert len(charges) == 1
        assert len(deposits) == 1

    def test_best_match_selected(self):
        """One charge, two deposits with close amounts -> closest amount wins."""
        charges = [_make_charge(amount=Decimal("100.00"), charge_date=date(2026, 3, 1))]
        deposits = [
            _make_deposit(id="d1", amount=Decimal("100.50"), transaction_date=date(2026, 3, 2)),
            _make_deposit(id="d2", amount=Decimal("100.01"), transaction_date=date(2026, 3, 2)),
        ]
        results = fuzzy_match(charges, deposits)
        assert len(results) == 1
        matched = results[0]
        assert matched.match_type == "fuzzy"
        assert matched.deposit is not None
        assert matched.deposit.amount == Decimal("100.01")
        # Charge list empty, one deposit remains
        assert len(charges) == 0
        assert len(deposits) == 1

    def test_amount_within_2_percent(self):
        """$1000 charge, $1015 deposit -> fuzzy match (1.5% off, within 2% and $50)."""
        charges = [_make_charge(amount=Decimal("1000.00"), charge_date=date(2026, 3, 1))]
        deposits = [_make_deposit(amount=Decimal("1015.00"), transaction_date=date(2026, 3, 1))]
        results = fuzzy_match(charges, deposits)
        assert len(results) == 1
        assert results[0].match_type == "fuzzy"
        assert results[0].confidence >= Decimal("0.40")

    def test_amount_over_2_percent(self):
        """$1000 charge, $1030 deposit -> unmatched (3% off, exceeds 2% tolerance)."""
        charges = [_make_charge(amount=Decimal("1000.00"), charge_date=date(2026, 3, 1))]
        deposits = [_make_deposit(amount=Decimal("1030.00"), transaction_date=date(2026, 3, 1))]
        results = fuzzy_match(charges, deposits)
        assert len(results) == 0
        assert len(charges) == 1
        assert len(deposits) == 1
