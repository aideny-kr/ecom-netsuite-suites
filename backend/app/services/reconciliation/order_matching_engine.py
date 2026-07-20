"""Order-level matching engine."""

from __future__ import annotations

from decimal import Decimal

from app.schemas.order_reconciliation import (
    ChargeRecord,
    NSPaymentRecord,
    OrderMatchCandidate,
)

# Back-compat re-export: extraction now lives in the shared, tenant-configurable
# ``order_ref`` module (R3 Part 1). Existing call sites / tests that import
# ``extract_order_ref`` from here keep working.
from app.services.reconciliation.order_ref import extract_order_ref  # noqa: F401


class OrderMatchingEngine:
    """Three-tier order matching: deterministic → fuzzy → unmatched."""

    _AMOUNT_TOLERANCE = Decimal("0.50")
    # An ambiguous same-ref pick (gate-round-2) is capped below the 0.95
    # auto_match threshold in OrderReconJob._store_results — human eyes only.
    _AMBIGUOUS_CONFIDENCE_CAP = Decimal("0.85")

    def match(
        self,
        charges: list[ChargeRecord],
        deposits: list[NSPaymentRecord],
    ) -> list[OrderMatchCandidate]:
        """Match charges to deposits. Returns all results including unmatched."""
        results: list[OrderMatchCandidate] = []
        unmatched_charges = list(charges)
        unmatched_deposits = list(deposits)

        # Tier 1: Deterministic — match on order_reference
        results.extend(self._deterministic_match(unmatched_charges, unmatched_deposits))

        # Tier 2: Fuzzy — amount + date + currency (stub for Task 4)
        results.extend(self._fuzzy_match(unmatched_charges, unmatched_deposits))

        # Remaining unmatched charges — these are the real exceptions
        # (Stripe charged the customer but NetSuite has no matching deposit)
        for c in unmatched_charges:
            results.append(
                OrderMatchCandidate(
                    charge=c,
                    deposit=None,
                    match_type="unmatched",
                    confidence=Decimal("0"),
                    variance_amount=c.amount,
                    variance_type="missing_in_netsuite",
                    variance_explanation=(
                        f"Stripe charge {c.order_reference or c.source_id} has no matching NetSuite deposit"
                    ),
                )
            )

        # NOTE: Unmatched deposits are NOT reported as exceptions.
        # Deposits without matching charges are expected (non-Stripe payments,
        # deposits from outside the date range, manual adjustments).
        # The goal is one-directional: every Stripe charge should be in NetSuite.

        return results

    def _deterministic_match(
        self,
        unmatched_charges: list[ChargeRecord],
        unmatched_deposits: list[NSPaymentRecord],
    ) -> list[OrderMatchCandidate]:
        """Tier 1: Match on exact order_reference equality.

        A ref can be shared by several charges and/or several deposits (split
        orders across payout lines, or a deposit plus a correction/reversal —
        more likely now that the ref-keyed fetch pulls deposits from a much
        wider date range). Each ref's whole group is handed to
        ``_match_same_ref_group`` for set-to-set pairing (see its docstring),
        then leaves the pool entirely — a same-ref sibling can never leak
        into tier-2 fuzzy matching for an unrelated charge.

        Mutates unmatched_charges and unmatched_deposits in place,
        removing matched items.
        """
        results: list[OrderMatchCandidate] = []

        charges_by_ref: dict[str, list[ChargeRecord]] = {}
        for c in unmatched_charges:
            if c.order_reference:
                charges_by_ref.setdefault(c.order_reference, []).append(c)

        deposits_by_ref: dict[str, list[NSPaymentRecord]] = {}
        for d in unmatched_deposits:
            if d.order_reference:
                deposits_by_ref.setdefault(d.order_reference, []).append(d)

        matched_charges: list[ChargeRecord] = []
        consumed_deposits: list[NSPaymentRecord] = []

        for ref, charge_group in charges_by_ref.items():
            deposit_group = deposits_by_ref.get(ref)
            if not deposit_group:
                continue

            group_results, group_matched_charges = self._match_same_ref_group(charge_group, deposit_group)
            results.extend(group_results)
            matched_charges.extend(group_matched_charges)
            # The whole same-ref group leaves the pool — deposits left over
            # after pairing belong to this order's evidence, not to some
            # other charge's fuzzy match.
            consumed_deposits.extend(deposit_group)

        # Remove matched items from unmatched lists
        for c in matched_charges:
            unmatched_charges.remove(c)
        for d in consumed_deposits:
            unmatched_deposits.remove(d)

        return results

    def _match_same_ref_group(
        self,
        charge_group: list[ChargeRecord],
        deposit_group: list[NSPaymentRecord],
    ) -> tuple[list[OrderMatchCandidate], list[ChargeRecord]]:
        """Match one order_reference's charges against its deposits SET-to-SET.

        For a ref shared by M charges and N deposits: (1) within each amount
        bucket, pair charge<->deposit confidently ONLY when the bucket has
        an EQUAL count of charges and deposits — this handles legitimate
        split/partial-capture orders (2 charges + 2 deposits, both exact)
        with full confidence; a surplus OR a deficit for that amount are
        both competing-candidates situations (which one is the real one?)
        and defer the whole bucket to step 2; (2) each remaining charge
        takes the nearest-transaction-date remaining deposit (tie: lowest
        netsuite_internal_id) but the pick is AMBIGUOUS — it must never
        auto-match; (3) leftover same-ref deposits after pairing
        (reversals/duplicates) are fenced from unrelated fuzzy matching and
        recorded as ``same_ref_deposit_ids`` evidence on every result of the
        group; (4) leftover same-ref charges (more charges than deposits)
        fall through to fuzzy/missing exactly like no-ref charges. Ambiguity
        = human eyes: an ambiguous pick is capped below auto-match and routed
        to needs_review.

        The single-charge single-deposit case is the pre-existing behavior
        byte-identical — always paired directly, amount variance or not,
        never flagged ambiguous (there is no second candidate to be ambiguous
        about).

        Returns (results, matched_charges) — matched_charges lists only the
        charges this group actually resolved (confidently or ambiguously) so
        the caller can remove them from the unmatched pool; a leftover charge
        (point 4) is NOT included, so it falls through to fuzzy/missing.
        """
        if len(charge_group) == 1 and len(deposit_group) == 1:
            charge, deposit = charge_group[0], deposit_group[0]
            variance, confidence, variance_type = self._variance_and_confidence(charge, deposit)
            return [
                OrderMatchCandidate(
                    charge=charge,
                    deposit=deposit,
                    match_type="deterministic",
                    confidence=confidence,
                    variance_amount=variance,
                    variance_type=variance_type,
                    match_rule="order_reference_exact",
                )
            ], [charge]

        remaining_deposits = list(deposit_group)
        remaining_charges: list[ChargeRecord] = []
        # (charge, deposit, ambiguous) — ambiguous=False for step-1 confident
        # exact-amount pairs, True for step-2 nearest-date picks.
        assigned: list[tuple[ChargeRecord, NSPaymentRecord, bool]] = []

        # Step 1: confident exact-amount pairing, per amount bucket. A bucket
        # only pairs confidently when charges and deposits are EQUAL in
        # count for that amount — a surplus OR a deficit both mean competing
        # candidates (which one is the real one?) and are inherently
        # ambiguous, so either direction defers the whole bucket to step 2.
        # Sorted by stable id keys before zipping so attribution (which
        # charge lands on which deposit) never depends on DB fetch order.
        charges_by_amount: dict[Decimal, list[ChargeRecord]] = {}
        for c in charge_group:
            charges_by_amount.setdefault(c.amount, []).append(c)

        for amount, c_list in charges_by_amount.items():
            d_list = [d for d in remaining_deposits if d.amount == amount]
            if d_list and len(d_list) == len(c_list):
                sorted_charges = sorted(c_list, key=lambda c: c.source_id)
                sorted_deposits = sorted(d_list, key=lambda d: self._numeric_id_sort_key(d.netsuite_internal_id))
                for c, d in zip(sorted_charges, sorted_deposits):
                    assigned.append((c, d, False))
                    remaining_deposits.remove(d)
            else:
                remaining_charges.extend(c_list)

        # Step 2: each remaining charge takes the nearest-date remaining
        # deposit — ambiguous, never auto-matched. Sorted by source_id first
        # so the processing order — and thus which charge wins a deposit
        # deficit — never depends on charge_group's original (DB fetch)
        # order.
        remaining_charges.sort(key=lambda c: c.source_id)
        for c in remaining_charges:
            if not remaining_deposits:
                break
            chosen = min(remaining_deposits, key=lambda d, _c=c: self._nearest_deposit_sort_key(d, _c))
            remaining_deposits.remove(chosen)
            assigned.append((c, chosen, True))

        # Step 3/4: whatever deposits are left are fenced as group-wide
        # evidence; whatever charges are left (more charges than deposits)
        # simply aren't in `assigned` and fall through to fuzzy/missing.
        leftover_deposit_ids = [d.id for d in remaining_deposits]

        results: list[OrderMatchCandidate] = []
        matched_charges: list[ChargeRecord] = []
        for c, d, ambiguous in assigned:
            variance, confidence, variance_type = self._variance_and_confidence(c, d)
            if ambiguous:
                confidence = min(confidence, self._AMBIGUOUS_CONFIDENCE_CAP)

            results.append(
                OrderMatchCandidate(
                    charge=c,
                    deposit=d,
                    match_type="deterministic",
                    confidence=confidence,
                    variance_amount=variance,
                    variance_type=variance_type,
                    match_rule="order_reference_exact",
                    same_ref_deposit_ids=list(leftover_deposit_ids),
                    ambiguous_same_ref=ambiguous,
                )
            )
            matched_charges.append(c)

        return results, matched_charges

    def _variance_and_confidence(
        self,
        charge: ChargeRecord,
        deposit: NSPaymentRecord,
    ) -> tuple[Decimal, Decimal, str | None]:
        """Returns (variance_amount, confidence, variance_type) for a pair,
        before any ambiguous-pick cap is applied."""
        variance = abs(charge.amount - deposit.amount)
        if variance == Decimal("0"):
            return variance, Decimal("1.0"), None
        if variance <= self._AMOUNT_TOLERANCE:
            return variance, Decimal("0.95"), "amount_mismatch"
        return variance, Decimal("0.90"), "amount_mismatch"

    @staticmethod
    def _numeric_id_sort_key(id_value: str | None) -> tuple[int, int | str]:
        """Sort key for a NetSuite internal id: numeric ids (the real-world
        case) sort numerically; any non-numeric id sorts after all numeric
        ones. The leading 0/1 keeps the two branches from ever comparing an
        int against a str. Shared by step 2's nearest-date tie-break and
        step 1's stable equal-count zip attribution."""
        try:
            return (0, int(id_value))
        except (TypeError, ValueError):
            return (1, id_value or "")

    @classmethod
    def _nearest_deposit_sort_key(
        cls,
        deposit: NSPaymentRecord,
        charge: ChargeRecord,
    ) -> tuple[int, tuple[int, int | str]]:
        """Sort key for step 2's ambiguous pick: nearest transaction_date to
        the charge's date wins, tie-broken by the lowest netsuite_internal_id
        so the outcome never depends on fetch/iteration order."""
        days_apart = abs((deposit.transaction_date - charge.charge_date).days)
        return (days_apart, cls._numeric_id_sort_key(deposit.netsuite_internal_id))

    def _fuzzy_match(
        self,
        unmatched_charges: list[ChargeRecord],
        unmatched_deposits: list[NSPaymentRecord],
    ) -> list[OrderMatchCandidate]:
        """Tier 2: Fuzzy matching on amount + date + currency."""
        from app.services.reconciliation.order_fuzzy_matcher import fuzzy_match

        return fuzzy_match(unmatched_charges, unmatched_deposits)
