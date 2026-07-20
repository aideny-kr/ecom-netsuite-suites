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

        Several deposits can legitimately share one order_reference (an
        original posting plus a later correction/reversal — more likely now
        that the ref-keyed fetch pulls deposits from a much wider date range).
        When that happens, ``_select_same_ref_deposit`` picks one
        deterministically and the rest are treated as consumed alongside it —
        they leave the pool entirely so a same-ref sibling can never leak into
        tier-2 fuzzy matching for an unrelated charge.

        Mutates unmatched_charges and unmatched_deposits in place,
        removing matched items.
        """
        results: list[OrderMatchCandidate] = []

        # Index ALL deposits sharing an order_reference (not just the last one
        # seen), so a collision is detected rather than silently dropped.
        deposits_by_ref: dict[str, list[NSPaymentRecord]] = {}
        for d in unmatched_deposits:
            if d.order_reference:
                deposits_by_ref.setdefault(d.order_reference, []).append(d)

        matched_charges: list[ChargeRecord] = []
        consumed_deposits: list[NSPaymentRecord] = []

        for c in unmatched_charges:
            if not c.order_reference:
                continue
            candidates = deposits_by_ref.get(c.order_reference)
            if not candidates:
                continue

            if len(candidates) == 1:
                d = candidates[0]
                same_ref_deposit_ids: list[str] = []
            else:
                d, same_ref_deposit_ids = self._select_same_ref_deposit(c, candidates)

            variance = abs(c.amount - d.amount)
            if variance == Decimal("0"):
                confidence = Decimal("1.0")
                variance_type = None
            elif variance <= self._AMOUNT_TOLERANCE:
                confidence = Decimal("0.95")
                variance_type = "amount_mismatch"
            else:
                confidence = Decimal("0.90")
                variance_type = "amount_mismatch"

            results.append(
                OrderMatchCandidate(
                    charge=c,
                    deposit=d,
                    match_type="deterministic",
                    confidence=confidence,
                    variance_amount=variance,
                    variance_type=variance_type,
                    match_rule="order_reference_exact",
                    same_ref_deposit_ids=same_ref_deposit_ids,
                )
            )

            matched_charges.append(c)
            # The whole same-ref group leaves the pool — the non-chosen
            # sibling(s) belong to this order, not to some other charge's
            # fuzzy match.
            consumed_deposits.extend(candidates)
            del deposits_by_ref[c.order_reference]

        # Remove matched items from unmatched lists
        for c in matched_charges:
            unmatched_charges.remove(c)
        for d in consumed_deposits:
            unmatched_deposits.remove(d)

        return results

    @staticmethod
    def _select_same_ref_deposit(
        charge: ChargeRecord,
        candidates: list[NSPaymentRecord],
    ) -> tuple[NSPaymentRecord, list[str]]:
        """Deterministically pick one deposit among several sharing a charge's
        order_reference (e.g. an original posting plus a correction/reversal).

        Selection rule: exactly one amount-exact candidate wins outright; with
        zero or multiple amount-exact candidates, the nearest transaction_date
        to the charge's date wins, tie-broken by the lowest netsuite_internal_id
        so the outcome never depends on fetch/iteration order.

        Returns (chosen, other_ids) where other_ids lists the non-chosen
        candidates' ids for collision evidence.
        """
        exact = [d for d in candidates if d.amount == charge.amount]
        if len(exact) == 1:
            chosen = exact[0]
        else:
            pool = exact if exact else candidates

            def sort_key(d: NSPaymentRecord) -> tuple[int, tuple[int, int | str]]:
                days_apart = abs((d.transaction_date - charge.charge_date).days)
                # Numeric ids (the real-world case) sort numerically; any
                # non-numeric id sorts after all numeric ones. The leading
                # 0/1 keeps the two branches from ever comparing an int
                # against a str within the same days_apart tie.
                try:
                    tie_break: tuple[int, int | str] = (0, int(d.netsuite_internal_id))
                except (TypeError, ValueError):
                    tie_break = (1, d.netsuite_internal_id or "")
                return (days_apart, tie_break)

            chosen = min(pool, key=sort_key)

        other_ids = [d.id for d in candidates if d.id != chosen.id]
        return chosen, other_ids

    def _fuzzy_match(
        self,
        unmatched_charges: list[ChargeRecord],
        unmatched_deposits: list[NSPaymentRecord],
    ) -> list[OrderMatchCandidate]:
        """Tier 2: Fuzzy matching on amount + date + currency."""
        from app.services.reconciliation.order_fuzzy_matcher import fuzzy_match

        return fuzzy_match(unmatched_charges, unmatched_deposits)
