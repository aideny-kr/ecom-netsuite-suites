"""Fuzzy matching for order-level reconciliation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.schemas.order_reconciliation import (
    ChargeRecord,
    NSPaymentRecord,
    OrderMatchCandidate,
)

_DATE_WINDOW_DAYS = 5
_AMOUNT_PCT_TOLERANCE = Decimal("0.02")  # 2%
_AMOUNT_ABS_TOLERANCE = Decimal("50.00")  # $50 max
_MAX_FUZZY_CONFIDENCE = Decimal("0.89")


def _amount_proximity(charge_amount: Decimal, deposit_amount: Decimal) -> Decimal:
    """Score 0-1 based on how close the amounts are. 1 = exact match."""
    if charge_amount == 0 and deposit_amount == 0:
        return Decimal("1")
    if charge_amount == 0:
        return Decimal("0")
    diff = abs(charge_amount - deposit_amount)
    pct_diff = diff / charge_amount
    # If beyond tolerance, return 0
    if pct_diff > _AMOUNT_PCT_TOLERANCE or diff > _AMOUNT_ABS_TOLERANCE:
        return Decimal("0")
    # Linear scale: 0% diff → 1.0, 2% diff → 0.0
    return Decimal("1") - (pct_diff / _AMOUNT_PCT_TOLERANCE)


def _date_proximity(charge_date: date, deposit_date: date) -> Decimal:
    """Score 0-1 based on how close the dates are. 1 = same day."""
    days_apart = abs((charge_date - deposit_date).days)
    if days_apart > _DATE_WINDOW_DAYS:
        return Decimal("0")
    return Decimal("1") - Decimal(str(days_apart)) / Decimal(str(_DATE_WINDOW_DAYS))


def fuzzy_match(
    unmatched_charges: list[ChargeRecord],
    unmatched_deposits: list[NSPaymentRecord],
) -> list[OrderMatchCandidate]:
    """Match charges to deposits by amount + date + currency proximity.

    Mutates the input lists in place — matched items are removed.
    """
    results: list[OrderMatchCandidate] = []

    # Build candidates for each charge
    matched_charge_indices: list[int] = []
    matched_deposit_indices: set[int] = set()

    # Score all charge-deposit pairs, then greedily assign best matches
    charge_best: list[tuple[int, int, Decimal]] = []  # (charge_idx, deposit_idx, score)

    for ci, charge in enumerate(unmatched_charges):
        best_score = Decimal("0")
        best_di = -1

        for di, deposit in enumerate(unmatched_deposits):
            # Currency must match exactly
            if charge.currency.upper() != deposit.currency.upper():
                continue

            # Check date proximity
            date_score = _date_proximity(charge.charge_date, deposit.transaction_date)
            if date_score == 0:
                continue

            # Check amount proximity
            amount_score = _amount_proximity(charge.amount, deposit.amount)
            if amount_score == 0:
                continue

            # Composite score: amount (0-0.5) + date (0-0.3) + base 0.2
            score = amount_score * Decimal("0.5") + date_score * Decimal("0.3") + Decimal("0.2")
            score = min(score, _MAX_FUZZY_CONFIDENCE)

            if score > best_score:
                best_score = score
                best_di = di

        if best_di >= 0:
            charge_best.append((ci, best_di, best_score))

    # Sort by score descending to assign best matches first
    charge_best.sort(key=lambda x: x[2], reverse=True)

    for ci, di, score in charge_best:
        if ci in matched_charge_indices or di in matched_deposit_indices:
            continue
        matched_charge_indices.append(ci)
        matched_deposit_indices.add(di)

        charge = unmatched_charges[ci]
        deposit = unmatched_deposits[di]
        variance = abs(charge.amount - deposit.amount)

        results.append(
            OrderMatchCandidate(
                charge=charge,
                deposit=deposit,
                match_type="fuzzy",
                confidence=score,
                variance_amount=variance,
                variance_type="amount_mismatch" if variance > 0 else None,
                variance_explanation=(
                    f"Fuzzy match: amounts differ by {variance}" if variance > 0 else "Fuzzy match: exact amount"
                ),
                match_rule="amount+date+currency",
            )
        )

    # Remove matched items from lists (reverse order to preserve indices)
    for ci in sorted(matched_charge_indices, reverse=True):
        unmatched_charges.pop(ci)
    for di in sorted(matched_deposit_indices, reverse=True):
        unmatched_deposits.pop(di)

    return results
