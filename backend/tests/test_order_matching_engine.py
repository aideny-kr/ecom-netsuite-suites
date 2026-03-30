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
        assert results[0].variance_type == "missing"

    def test_unmatched_deposit(self):
        from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

        engine = OrderMatchingEngine()
        deposits = [_make_deposit(order_reference="R999999999")]
        results = engine.match([], deposits)
        unmatched = [r for r in results if r.deposit and r.deposit.order_reference == "R999999999"]
        assert len(unmatched) == 1
        assert unmatched[0].match_type == "unmatched"

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
