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

    def test_single_charge_single_deposit_variance_byte_identical(self):
        """Regression guard: a same-ref group with exactly one charge and one
        deposit must NEVER be routed through the ambiguous nearest-date path,
        even when the amounts don't match exactly — this is the plain
        variance case, not a collision. (Pre-existing behavior the round-2
        set-to-set rewrite must not disturb.)"""
        from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

        engine = OrderMatchingEngine()
        charges = [_make_charge(order_reference="R100000005", amount=Decimal("100.00"))]
        deposits = [_make_deposit(order_reference="R100000005", amount=Decimal("95.00"))]
        results = engine.match(charges, deposits)
        assert results[0].match_type == "deterministic"
        assert results[0].variance_amount == Decimal("5.00")
        assert results[0].confidence == Decimal("0.90")
        assert results[0].ambiguous_same_ref is False
        assert results[0].same_ref_deposit_ids == []


class TestSameRefSetToSetMatching:
    """Gate-round-2 design: a ref shared by M charges and N deposits matches
    SET-to-SET, not by picking one deposit for a single charge. Split orders
    (multiple legitimate charges sharing one order_reference) must all match,
    and any pick that isn't a clean 1:1 exact-amount pairing is flagged
    ambiguous and routed to human review — never auto-matched.
    """

    def test_split_order_two_charges_two_deposits_both_exact(self):
        """2 charges (100, 50) + 2 deposits (100, 50) sharing a ref: BOTH
        match amount-exact, full confidence, no ambiguity, nothing missing.
        (The round-2 regression: the second charge used to be falsely
        reported missing because the first charge's match consumed the
        entire same-ref deposit group.)"""
        from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

        engine = OrderMatchingEngine()
        charge_a = _make_charge(id="c_a", order_reference="R200000001", amount=Decimal("100.00"))
        charge_b = _make_charge(id="c_b", order_reference="R200000001", amount=Decimal("50.00"))
        deposit_a = _make_deposit(id="d_a", order_reference="R200000001", amount=Decimal("100.00"))
        deposit_b = _make_deposit(id="d_b", order_reference="R200000001", amount=Decimal("50.00"))

        results = engine.match([charge_a, charge_b], [deposit_a, deposit_b])

        assert len(results) == 2
        by_charge_id = {r.charge.id: r for r in results}
        for charge_id, expected_deposit_id in (("c_a", "d_a"), ("c_b", "d_b")):
            r = by_charge_id[charge_id]
            assert r.match_type == "deterministic"
            assert r.deposit.id == expected_deposit_id
            assert r.variance_amount == Decimal("0")
            assert r.confidence == Decimal("1.0")
            assert r.ambiguous_same_ref is False
            assert r.same_ref_deposit_ids == []

    def test_three_charges_two_deposits_third_falls_through(self):
        """3 charges + 2 deposits sharing a ref: two pair exact, the third
        (no deposit left in its amount bucket) falls through to fuzzy/missing
        exactly like a no-ref charge."""
        from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

        engine = OrderMatchingEngine()
        charge_a = _make_charge(id="c_a", order_reference="R200000002", amount=Decimal("100.00"))
        charge_b = _make_charge(id="c_b", order_reference="R200000002", amount=Decimal("50.00"))
        charge_c = _make_charge(id="c_c", order_reference="R200000002", amount=Decimal("30.00"))
        deposit_a = _make_deposit(id="d_a", order_reference="R200000002", amount=Decimal("100.00"))
        deposit_b = _make_deposit(id="d_b", order_reference="R200000002", amount=Decimal("50.00"))

        results = engine.match([charge_a, charge_b, charge_c], [deposit_a, deposit_b])

        by_charge_id = {r.charge.id: r for r in results}
        assert by_charge_id["c_a"].match_type == "deterministic"
        assert by_charge_id["c_a"].deposit.id == "d_a"
        assert by_charge_id["c_b"].match_type == "deterministic"
        assert by_charge_id["c_b"].deposit.id == "d_b"
        # No deposit left for c_c's amount bucket, and no deposits remain at
        # all in the group — falls through unresolved, like a no-ref charge.
        assert by_charge_id["c_c"].match_type == "unmatched"
        assert by_charge_id["c_c"].variance_type == "missing_in_netsuite"

    def test_ambiguous_pick_nearest_date_wins_capped_below_auto_match(self):
        """1 charge, 2 non-exact deposits sharing a ref: nearest date wins,
        but the pick is ambiguous — confidence capped at 0.85 (below the 0.95
        auto_match threshold), flagged, and the loser fenced as evidence."""
        from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

        engine = OrderMatchingEngine()
        charge = _make_charge(
            order_reference="R200000003",
            amount=Decimal("100.00"),
            charge_date=date(2026, 3, 10),
        )
        deposit_near = _make_deposit(
            id="d_near",
            order_reference="R200000003",
            amount=Decimal("90.00"),
            transaction_date=date(2026, 3, 11),
        )
        deposit_far = _make_deposit(
            id="d_far",
            order_reference="R200000003",
            amount=Decimal("80.00"),
            transaction_date=date(2026, 2, 1),
        )

        results = engine.match([charge], [deposit_far, deposit_near])

        assert len(results) == 1
        r = results[0]
        assert r.match_type == "deterministic"
        assert r.deposit.id == "d_near"
        assert r.ambiguous_same_ref is True
        assert r.confidence == Decimal("0.85")
        assert r.same_ref_deposit_ids == ["d_far"]

    def test_zero_variance_ambiguous_pick_two_exact_candidates(self):
        """1 charge, 2 deposits that BOTH equal the charge's amount exactly:
        still a coin flip (which one is the real one?) — nearest date wins
        among the exacts, but the pick stays ambiguous even though variance
        is zero."""
        from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

        engine = OrderMatchingEngine()
        charge = _make_charge(
            order_reference="R200000004",
            amount=Decimal("100.00"),
            charge_date=date(2026, 3, 10),
        )
        deposit_near = _make_deposit(
            id="d_near_exact",
            order_reference="R200000004",
            amount=Decimal("100.00"),
            transaction_date=date(2026, 3, 11),
        )
        deposit_far = _make_deposit(
            id="d_far_exact",
            order_reference="R200000004",
            amount=Decimal("100.00"),
            transaction_date=date(2026, 2, 1),
        )

        results = engine.match([charge], [deposit_far, deposit_near])

        assert len(results) == 1
        r = results[0]
        assert r.match_type == "deterministic"
        assert r.deposit.id == "d_near_exact"
        assert r.variance_amount == Decimal("0")
        assert r.ambiguous_same_ref is True
        assert r.confidence == Decimal("0.85")
        assert r.same_ref_deposit_ids == ["d_far_exact"]

    def test_deposit_deficit_two_charges_one_deposit_is_ambiguous_not_confident(self):
        """Mirror of the surplus case: 2 same-amount charges sharing a ref
        with only 1 exact deposit is ALSO a competing-candidates situation
        (which charge is the real one?) — it must never resolve at
        confidence 1.0. Exactly one charge wins the ambiguous nearest-date
        pick (capped at 0.85); the other has no deposit left and falls
        through to fuzzy/missing."""
        from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

        charge_a = _make_charge(id="c_a", source_id="ch_a", order_reference="R400000001", amount=Decimal("100.00"))
        charge_b = _make_charge(id="c_b", source_id="ch_b", order_reference="R400000001", amount=Decimal("100.00"))
        deposit_solo = _make_deposit(
            id="d_solo",
            netsuite_internal_id="999",
            order_reference="R400000001",
            amount=Decimal("100.00"),
        )

        engine = OrderMatchingEngine()
        results = engine.match([charge_a, charge_b], [deposit_solo])

        assert all(r.confidence != Decimal("1.0") for r in results)
        ambiguous_results = [r for r in results if r.ambiguous_same_ref]
        unmatched_results = [r for r in results if r.match_type == "unmatched"]
        assert len(ambiguous_results) == 1
        assert len(unmatched_results) == 1
        assert ambiguous_results[0].deposit.id == "d_solo"
        assert ambiguous_results[0].confidence == Decimal("0.85")

    def test_deposit_deficit_winner_deterministic_regardless_of_charge_order(self):
        """Which charge wins the deficit's ambiguous pick must not depend on
        DB fetch order — it's decided by a stable key (source_id), not
        whichever happened to be first/last in the input list."""
        from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

        charge_a = _make_charge(id="c_a", source_id="ch_a", order_reference="R400000002", amount=Decimal("100.00"))
        charge_b = _make_charge(id="c_b", source_id="ch_b", order_reference="R400000002", amount=Decimal("100.00"))
        deposit_solo = _make_deposit(
            id="d_solo",
            netsuite_internal_id="999",
            order_reference="R400000002",
            amount=Decimal("100.00"),
        )

        outcomes = []
        for charges in ([charge_a, charge_b], [charge_b, charge_a]):
            engine = OrderMatchingEngine()
            results = engine.match(list(charges), [deposit_solo])
            winner = next(r for r in results if r.ambiguous_same_ref)
            outcomes.append(winner.charge.id)

        assert outcomes[0] == outcomes[1]

    def test_equal_count_zip_stable_attribution_regardless_of_fetch_order(self):
        """2 same-amount charges + 2 exact deposits sharing a ref: the
        charge<->deposit pairing must be identical no matter which order
        the charges/deposits list arrives in (DB fetch order is not
        guaranteed) — attribution stability for the audit trail."""
        from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

        charge_a = _make_charge(id="c_a", source_id="ch_a", order_reference="R400000003", amount=Decimal("100.00"))
        charge_b = _make_charge(id="c_b", source_id="ch_b", order_reference="R400000003", amount=Decimal("100.00"))
        deposit_p = _make_deposit(
            id="d_p", netsuite_internal_id="600", order_reference="R400000003", amount=Decimal("100.00")
        )
        deposit_q = _make_deposit(
            id="d_q", netsuite_internal_id="500", order_reference="R400000003", amount=Decimal("100.00")
        )

        pairings = []
        for charges, deposits in (
            ([charge_a, charge_b], [deposit_p, deposit_q]),
            ([charge_b, charge_a], [deposit_q, deposit_p]),
        ):
            engine = OrderMatchingEngine()
            results = engine.match(list(charges), list(deposits))
            assert len(results) == 2
            assert all(r.confidence == Decimal("1.0") and not r.ambiguous_same_ref for r in results)
            pairings.append({r.charge.id: r.deposit.id for r in results})

        assert pairings[0] == pairings[1]
