"""Tests for the deterministic + fuzzy matching engine."""

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.schemas.reconciliation import DepositRecord, MatchCandidate, PayoutRecord
from app.services.reconciliation.matching_engine import MatchingEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _payout(
    source_id: str = "po_test001",
    amount: str = "1000.00",
    net_amount: str = "970.00",
    fee_amount: str = "30.00",
    arrival_date: date | None = date(2026, 3, 10),
    currency: str = "USD",
) -> PayoutRecord:
    return PayoutRecord(
        id=str(uuid.uuid4()),
        source_id=source_id,
        amount=Decimal(amount),
        net_amount=Decimal(net_amount),
        fee_amount=Decimal(fee_amount),
        currency=currency,
        arrival_date=arrival_date,
    )


def _deposit(
    netsuite_internal_id: str = "12001",
    amount: str = "970.00",
    transaction_date: date | None = date(2026, 3, 10),
    memo: str | None = "Stripe payout po_test001",
    related_payout_id: str | None = "po_test001",
    currency: str = "USD",
) -> DepositRecord:
    return DepositRecord(
        id=str(uuid.uuid4()),
        netsuite_internal_id=netsuite_internal_id,
        amount=Decimal(amount),
        currency=currency,
        transaction_date=transaction_date,
        memo=memo,
        related_payout_id=related_payout_id,
    )


# ---------------------------------------------------------------------------
# Deterministic matching tests
# ---------------------------------------------------------------------------
class TestDeterministicMatching:
    """Tier 1: Exact payout ID match + amount summation within tolerance."""

    def test_exact_match_payout_id(self):
        """Deposit has related_payout_id matching payout source_id and amount matches net."""
        engine = MatchingEngine()
        payout = _payout(source_id="po_exact01", net_amount="1455.00")
        deposit = _deposit(related_payout_id="po_exact01", amount="1455.00")

        results = engine.match([payout], [deposit])

        assert len(results) == 1
        assert results[0].match_type == "deterministic"
        assert results[0].confidence == Decimal("1.0")
        assert results[0].variance_amount == Decimal("0.00")

    def test_exact_match_payout_id_in_memo(self):
        """Deposit memo contains payout ID (no explicit related_payout_id)."""
        engine = MatchingEngine()
        payout = _payout(source_id="po_memo01", net_amount="2272.29")
        deposit = _deposit(
            related_payout_id=None,
            amount="2272.29",
            memo="Stripe payout po_memo01 deposit",
        )

        results = engine.match([payout], [deposit])

        assert len(results) == 1
        assert results[0].match_type == "deterministic"
        assert results[0].confidence >= Decimal("0.95")

    def test_exact_match_summation_within_tolerance(self):
        """Payout net matches deposit amount within rounding tolerance (0.05)."""
        engine = MatchingEngine()
        payout = _payout(source_id="po_round01", net_amount="1500.00")
        deposit = _deposit(
            related_payout_id="po_round01",
            amount="1500.02",
        )

        results = engine.match([payout], [deposit])

        assert len(results) == 1
        assert results[0].match_type == "deterministic"
        assert results[0].confidence >= Decimal("0.95")

    def test_no_match_wrong_currency(self):
        """Payout and deposit in different currencies should not deterministic match."""
        engine = MatchingEngine()
        payout = _payout(source_id="po_fx01", net_amount="1000.00", currency="USD")
        deposit = _deposit(related_payout_id="po_fx01", amount="1000.00", currency="EUR")

        results = engine.match([payout], [deposit])

        # Should NOT produce a deterministic match (currency mismatch)
        matched = [r for r in results if r.match_type == "deterministic"]
        assert len(matched) == 0

    def test_matching_is_idempotent(self):
        """Running matching twice with same data produces identical results."""
        engine = MatchingEngine()
        payout = _payout(source_id="po_idem01", net_amount="500.00")
        deposit = _deposit(related_payout_id="po_idem01", amount="500.00")

        results_1 = engine.match([payout], [deposit])
        results_2 = engine.match([payout], [deposit])

        assert len(results_1) == len(results_2)
        for r1, r2 in zip(results_1, results_2):
            assert r1.match_type == r2.match_type
            assert r1.confidence == r2.confidence
            assert r1.variance_amount == r2.variance_amount

    def test_no_double_matching(self):
        """One deposit cannot be matched to two payouts."""
        engine = MatchingEngine()
        payout_a = _payout(source_id="po_dbl01", net_amount="500.00")
        payout_b = _payout(source_id="po_dbl02", net_amount="500.00")
        deposit = _deposit(related_payout_id="po_dbl01", amount="500.00")

        results = engine.match([payout_a, payout_b], [deposit])

        # Only one payout should match the deposit
        matched = [r for r in results if r.match_type != "unmatched"]
        matched_deposit_ids = [r.deposits[0].id for r in matched if r.deposits]
        assert len(matched_deposit_ids) == len(set(matched_deposit_ids)), "Deposit matched to multiple payouts"

    def test_unmatched_payout(self):
        """Payout with no matching deposit returns unmatched."""
        engine = MatchingEngine()
        payout = _payout(source_id="po_orphan01", net_amount="999.00")

        results = engine.match([payout], [])

        assert len(results) == 1
        assert results[0].match_type == "unmatched"
        assert results[0].confidence == Decimal("0")

    def test_unmatched_deposit(self):
        """Deposit with no matching payout returns unmatched."""
        engine = MatchingEngine()
        deposit = _deposit(related_payout_id=None, amount="1234.00", memo="Unknown deposit")

        results = engine.match([], [deposit])

        assert len(results) == 1
        assert results[0].match_type == "unmatched"
