"""Tests for standalone fuzzy matching utilities."""

from datetime import date
from decimal import Decimal

from app.services.reconciliation.fuzzy_matcher import (
    amount_within_tolerance,
    date_within_window,
    narration_similarity,
)


class TestAmountTolerance:
    def test_exact_match(self):
        assert amount_within_tolerance(Decimal("100.00"), Decimal("100.00")) is True

    def test_within_rounding(self):
        assert amount_within_tolerance(Decimal("100.00"), Decimal("100.03")) is True

    def test_outside_rounding_within_fx(self):
        result = amount_within_tolerance(Decimal("100.00"), Decimal("100.50"), fx_tolerance_pct=Decimal("0.01"))
        assert result is True

    def test_outside_all_tolerance(self):
        assert amount_within_tolerance(Decimal("100.00"), Decimal("105.00")) is False

    def test_zero_amounts(self):
        assert amount_within_tolerance(Decimal("0"), Decimal("0")) is True

    def test_negative_should_use_absolute(self):
        assert amount_within_tolerance(Decimal("100.00"), Decimal("99.97")) is True


class TestDateWindow:
    def test_same_day(self):
        assert date_within_window(date(2026, 3, 10), date(2026, 3, 10)) == 0

    def test_within_window(self):
        result = date_within_window(date(2026, 3, 10), date(2026, 3, 12))
        assert result == 2

    def test_outside_window(self):
        result = date_within_window(date(2026, 3, 10), date(2026, 3, 20), max_days=3)
        assert result == -1  # -1 = outside window

    def test_deposit_before_payout(self):
        """Deposit date before payout arrival is unusual but should still measure distance."""
        result = date_within_window(date(2026, 3, 12), date(2026, 3, 10))
        assert result == 2

    def test_none_dates(self):
        assert date_within_window(None, date(2026, 3, 10)) == -1
        assert date_within_window(date(2026, 3, 10), None) == -1


class TestNarrationSimilarity:
    def test_contains_payout_id(self):
        score = narration_similarity("Stripe payout po_abc123", "po_abc123")
        assert score >= 0.9

    def test_partial_overlap(self):
        score = narration_similarity("Bank deposit - Stripe batch", "Stripe payout po_xyz")
        assert 0.3 <= score <= 0.8

    def test_no_overlap(self):
        score = narration_similarity("Wire transfer from vendor", "po_abc123")
        assert score < 0.3

    def test_empty_memo(self):
        score = narration_similarity("", "po_abc123")
        assert score == 0.0

    def test_none_memo(self):
        score = narration_similarity(None, "po_abc123")
        assert score == 0.0
