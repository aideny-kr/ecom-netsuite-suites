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

        Mutates unmatched_charges and unmatched_deposits in place,
        removing matched items.
        """
        results: list[OrderMatchCandidate] = []

        # Index deposits by order_reference
        deposit_by_ref: dict[str, NSPaymentRecord] = {}
        for d in unmatched_deposits:
            if d.order_reference:
                deposit_by_ref[d.order_reference] = d

        matched_charges: list[ChargeRecord] = []
        matched_deposits: list[NSPaymentRecord] = []

        for c in unmatched_charges:
            if not c.order_reference:
                continue
            d = deposit_by_ref.get(c.order_reference)
            if d is None:
                continue

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
                )
            )

            matched_charges.append(c)
            matched_deposits.append(d)
            # Remove from deposit index so it can't double-match
            del deposit_by_ref[c.order_reference]

        # Remove matched items from unmatched lists
        for c in matched_charges:
            unmatched_charges.remove(c)
        for d in matched_deposits:
            unmatched_deposits.remove(d)

        return results

    def _fuzzy_match(
        self,
        unmatched_charges: list[ChargeRecord],
        unmatched_deposits: list[NSPaymentRecord],
    ) -> list[OrderMatchCandidate]:
        """Tier 2: Fuzzy matching on amount + date + currency."""
        from app.services.reconciliation.order_fuzzy_matcher import fuzzy_match

        return fuzzy_match(unmatched_charges, unmatched_deposits)
