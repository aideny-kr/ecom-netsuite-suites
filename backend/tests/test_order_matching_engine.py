"""TDD: Order-level reconciliation tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.schemas.order_reconciliation import (
    ChargeRecord,
    NSPaymentRecord,
    OrderMatchCandidate,
)


# --- Test helpers ---
def _make_charge(
    id="c1",
    source_id="ch_test",
    amount=Decimal("100.00"),
    fee=Decimal("3.20"),
    currency="USD",
    charge_date=date(2026, 3, 1),
    description=None,
    order_reference=None,
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
    id="d1",
    netsuite_internal_id="12345",
    amount=Decimal("100.00"),
    currency="USD",
    transaction_date=date(2026, 3, 1),
    record_type="custdep",
    memo=None,
    order_reference=None,
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


class TestSchemas:
    def test_charge_record(self):
        c = _make_charge(order_reference="R628489275")
        assert c.order_reference == "R628489275"

    def test_ns_payment_record(self):
        p = _make_deposit(order_reference="R628489275")
        assert p.record_type == "custdep"

    def test_order_match_candidate(self):
        m = OrderMatchCandidate(
            charge=_make_charge(),
            deposit=None,
            match_type="unmatched",
            confidence=Decimal("0"),
            variance_amount=Decimal("100.00"),
            variance_type="missing",
        )
        assert m.match_type == "unmatched"


class TestOrderReferenceExtraction:
    """Extract R\\d{9} from Stripe descriptions and NS sales order names."""

    def test_stripe_marketplace_order(self):
        from app.services.reconciliation.order_matching_engine import extract_order_ref

        assert extract_order_ref("Framework Marketplace Order ID: R628489275-XU9EPZPD") == "R628489275"

    def test_stripe_with_different_suffix(self):
        from app.services.reconciliation.order_matching_engine import extract_order_ref

        assert extract_order_ref("Framework Marketplace Order ID: R234917689-UZQLQUEA") == "R234917689"

    def test_netsuite_sales_order(self):
        from app.services.reconciliation.order_matching_engine import extract_order_ref

        assert extract_order_ref("Sales Order #R577684612") == "R577684612"

    def test_bare_order_number(self):
        from app.services.reconciliation.order_matching_engine import extract_order_ref

        assert extract_order_ref("R123456789") == "R123456789"

    def test_no_match(self):
        from app.services.reconciliation.order_matching_engine import extract_order_ref

        assert extract_order_ref("STRIPE PAYOUT") is None

    def test_none_input(self):
        from app.services.reconciliation.order_matching_engine import extract_order_ref

        assert extract_order_ref(None) is None

    def test_empty_string(self):
        from app.services.reconciliation.order_matching_engine import extract_order_ref

        assert extract_order_ref("") is None


class TestDeterministicMatching:
    def test_exact_order_ref_match(self):
        from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

        engine = OrderMatchingEngine()
        charges = [_make_charge(order_reference="R628489275", amount=Decimal("100.00"))]
        deposits = [_make_deposit(order_reference="R628489275", amount=Decimal("100.00"))]
        results = engine.match(charges, deposits)
        assert results[0].match_type == "deterministic"
        assert results[0].confidence >= Decimal("0.95")
        assert results[0].variance_amount == Decimal("0")

    def test_order_ref_match_with_amount_variance(self):
        from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

        engine = OrderMatchingEngine()
        charges = [_make_charge(order_reference="R628489275", amount=Decimal("100.00"))]
        deposits = [_make_deposit(order_reference="R628489275", amount=Decimal("95.00"))]
        results = engine.match(charges, deposits)
        assert results[0].match_type == "deterministic"
        assert results[0].variance_amount == Decimal("5.00")
        assert results[0].variance_type == "amount_mismatch"

    def test_no_ref_skips_deterministic(self):
        from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

        engine = OrderMatchingEngine()
        charges = [_make_charge(order_reference=None, amount=Decimal("100.00"))]
        deposits = [_make_deposit(order_reference=None, amount=Decimal("100.00"))]
        results = engine.match(charges, deposits)
        # Should fall through to fuzzy or unmatched, not deterministic
        assert all(r.match_type != "deterministic" for r in results)

    def test_unmatched_charge(self):
        from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

        engine = OrderMatchingEngine()
        charges = [_make_charge(order_reference="R999999999")]
        results = engine.match(charges, [])
        assert results[0].match_type == "unmatched"
        assert results[0].variance_type == "missing_in_netsuite"

    def test_unmatched_deposit_not_reported(self):
        """Unmatched deposits should NOT be reported as exceptions."""
        from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

        engine = OrderMatchingEngine()
        deposits = [_make_deposit(order_reference="R999999999")]
        results = engine.match([], deposits)
        # One-directional: only charges missing from NetSuite are flagged
        assert len(results) == 0

    def test_multiple_charges_multiple_deposits(self):
        from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

        engine = OrderMatchingEngine()
        charges = [
            _make_charge(id="c1", order_reference="R111111111", amount=Decimal("50.00")),
            _make_charge(id="c2", order_reference="R222222222", amount=Decimal("75.00")),
        ]
        deposits = [
            _make_deposit(id="d1", order_reference="R111111111", amount=Decimal("50.00")),
            _make_deposit(id="d2", order_reference="R222222222", amount=Decimal("75.00")),
        ]
        results = engine.match(charges, deposits)
        matched = [r for r in results if r.match_type == "deterministic"]
        assert len(matched) == 2


class TestSameRefDepositCollision:
    """Several deposits can legitimately share a charge's order_reference (an
    original posting plus a correction/reversal). Tier-1 must pick one
    deterministically — not silently keep whichever the dict-building loop
    iterated last — and must not leave the non-chosen sibling in the fuzzy
    pool for an unrelated charge to pick up.
    """

    def test_amount_exact_wins_regardless_of_deposit_iteration_order(self):
        """Two same-ref deposits, only one amount-exact: the exact one must win,
        independent of which order the deposits list presents them in."""
        from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

        charge = _make_charge(order_reference="R100000001", amount=Decimal("100.00"))
        deposit_exact = _make_deposit(
            id="d_exact",
            order_reference="R100000001",
            amount=Decimal("100.00"),
            transaction_date=date(2026, 3, 11),
        )
        # A same-ref sibling (e.g. a correction/reversal posting) with a
        # different amount and an earlier date.
        deposit_sibling = _make_deposit(
            id="d_sibling",
            order_reference="R100000001",
            amount=Decimal("150.00"),
            transaction_date=date(2026, 2, 20),
        )

        for deposits in ([deposit_exact, deposit_sibling], [deposit_sibling, deposit_exact]):
            engine = OrderMatchingEngine()
            results = engine.match([charge], list(deposits))
            assert len(results) == 1
            assert results[0].match_type == "deterministic"
            assert results[0].deposit.id == "d_exact"
            assert results[0].variance_amount == Decimal("0")

    def test_no_exact_amount_nearest_date_wins_and_evidence_carries_sibling(self):
        """Neither deposit is amount-exact: nearest transaction_date to the
        charge date wins, and the candidate records the non-chosen sibling's id
        so the collision is visible downstream (evidence)."""
        from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

        charge = _make_charge(
            order_reference="R100000002",
            amount=Decimal("100.00"),
            charge_date=date(2026, 3, 10),
        )
        deposit_near = _make_deposit(
            id="d_near",
            order_reference="R100000002",
            amount=Decimal("90.00"),
            transaction_date=date(2026, 3, 11),
        )
        deposit_far = _make_deposit(
            id="d_far",
            order_reference="R100000002",
            amount=Decimal("80.00"),
            transaction_date=date(2026, 2, 1),
        )

        engine = OrderMatchingEngine()
        results = engine.match([charge], [deposit_far, deposit_near])

        assert len(results) == 1
        assert results[0].match_type == "deterministic"
        assert results[0].deposit.id == "d_near"
        assert results[0].same_ref_deposit_ids == ["d_far"]

    def test_non_chosen_sibling_excluded_from_fuzzy_pool(self):
        """The non-chosen same-ref deposit must not leak into tier-2 fuzzy
        matching for an unrelated no-ref charge, even when its amount happens
        to coincide with that unrelated charge's amount."""
        from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

        charge_with_ref = _make_charge(
            id="c_ref",
            order_reference="R100000003",
            amount=Decimal("100.00"),
            charge_date=date(2026, 3, 10),
        )
        # No order_reference — a charge that would otherwise be eligible to
        # fuzzy-match the same-ref sibling below by amount + date + currency.
        charge_no_ref = _make_charge(
            id="c_no_ref",
            order_reference=None,
            amount=Decimal("50.00"),
            charge_date=date(2026, 3, 10),
        )
        deposit_exact = _make_deposit(
            id="d_exact",
            order_reference="R100000003",
            amount=Decimal("100.00"),
            transaction_date=date(2026, 3, 10),
        )
        # Shares charge_with_ref's order_reference, but its amount coincides
        # with charge_no_ref's amount — a tempting (wrong) fuzzy match.
        deposit_sibling = _make_deposit(
            id="d_sibling",
            order_reference="R100000003",
            amount=Decimal("50.00"),
            transaction_date=date(2026, 3, 10),
        )

        engine = OrderMatchingEngine()
        results = engine.match(
            [charge_with_ref, charge_no_ref],
            [deposit_exact, deposit_sibling],
        )

        by_charge_id = {r.charge.id: r for r in results}
        assert by_charge_id["c_ref"].match_type == "deterministic"
        assert by_charge_id["c_ref"].deposit.id == "d_exact"
        # charge_no_ref must NOT have fuzzy-matched deposit_sibling.
        assert by_charge_id["c_no_ref"].match_type == "unmatched"

    def test_single_deposit_per_ref_unaffected(self):
        """Regression: the common case (no collision) is byte-identical —
        same_ref_deposit_ids is empty."""
        from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

        engine = OrderMatchingEngine()
        charges = [_make_charge(order_reference="R100000004", amount=Decimal("100.00"))]
        deposits = [_make_deposit(order_reference="R100000004", amount=Decimal("100.00"))]
        results = engine.match(charges, deposits)
        assert results[0].match_type == "deterministic"
        assert results[0].same_ref_deposit_ids == []
