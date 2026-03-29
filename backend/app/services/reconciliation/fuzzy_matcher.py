"""Standalone fuzzy matching utilities for amount, date, and narration comparison.

All amounts use Decimal. No LLM involvement.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

_ROUNDING_TOLERANCE = Decimal("0.05")


def amount_within_tolerance(
    amount_a: Decimal,
    amount_b: Decimal,
    rounding_tolerance: Decimal = _ROUNDING_TOLERANCE,
    fx_tolerance_pct: Decimal | None = None,
) -> bool:
    """Check if two amounts are within acceptable tolerance.

    First checks rounding tolerance (absolute), then FX tolerance (percentage).
    """
    diff = abs(amount_a - amount_b)

    if diff <= rounding_tolerance:
        return True

    if fx_tolerance_pct is not None and amount_a > 0:
        pct_diff = diff / amount_a
        if pct_diff <= fx_tolerance_pct:
            return True

    return False


def date_within_window(
    arrival_date: date | None,
    transaction_date: date | None,
    max_days: int = 3,
) -> int:
    """Check if transaction_date is within max_days of arrival_date.

    Returns:
        Day difference (0 = same day) if within window, -1 if outside or dates are None.
    """
    if arrival_date is None or transaction_date is None:
        return -1

    diff = abs((transaction_date - arrival_date).days)
    if diff <= max_days:
        return diff
    return -1


def narration_similarity(memo: str | None, payout_source_id: str) -> float:
    """Score how well a deposit memo matches a payout reference.

    Returns a float 0.0-1.0:
    - 1.0: memo contains exact payout ID
    - 0.3-0.8: word overlap
    - 0.0: no overlap or empty memo
    """
    if not memo:
        return 0.0

    memo_lower = memo.lower()
    payout_lower = payout_source_id.lower()

    # Exact ID match in memo
    if payout_lower in memo_lower:
        return 1.0

    # Word overlap: intersection over smaller set (recall-biased)
    words_memo = set(re.findall(r"\w+", memo_lower))
    words_ref = set(re.findall(r"\w+", payout_lower))

    # Add common Stripe-related words for matching
    words_ref.update({"stripe", "payout"})

    if not words_memo or not words_ref:
        return 0.0

    intersection = words_memo & words_ref
    # Use recall-biased overlap: intersection / min(|A|, |B|)
    return len(intersection) / min(len(words_memo), len(words_ref))
