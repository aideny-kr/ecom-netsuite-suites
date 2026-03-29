"""Classify the type of variance between a Stripe payout and NetSuite deposit.

Taxonomy (from RECONCILIATION_SPEC.md):
- fees: Stripe processing fees not reflected in NetSuite
- fx_rounding: Small FX or rounding differences (<=0.05)
- timing: Amount matches but dates differ (T+1..T+3)
- missing: No counterpart on one side
- duplicate: Multiple deposits for one payout
- chargeback: Dispute-related variance
- manual_adjustment: Unexplained difference requiring investigation
"""

from __future__ import annotations

from decimal import Decimal

from app.schemas.reconciliation import DepositRecord, PayoutRecord

_ROUNDING_THRESHOLD = Decimal("0.05")
_FEE_VARIANCE_THRESHOLD = Decimal("0.50")  # If diff is close to fee_amount


def classify_variance(
    payout: PayoutRecord,
    deposit: DepositRecord | None,
    amount_diff: Decimal,
    day_diff: int,
    signals: list[str],
) -> tuple[str | None, str]:
    """Classify variance type and return (type, explanation).

    Returns (None, "") when there is no variance.
    """
    # No variance
    if amount_diff == Decimal("0") and day_diff == 0:
        return None, ""

    # Missing (no counterpart)
    if deposit is None:
        return "missing", f"No matching NetSuite deposit found for payout {payout.source_id}"

    # Fee variance: diff matches Stripe fee amount
    if "fee_variance" in signals:
        return (
            "fees",
            f"Variance of ${amount_diff} matches Stripe processing fee "
            f"(fee_amount={payout.fee_amount}). NetSuite may have recorded gross amount.",
        )

    fee_diff = abs(amount_diff - payout.fee_amount)
    if payout.fee_amount > 0 and fee_diff <= _FEE_VARIANCE_THRESHOLD:
        return (
            "fees",
            f"Variance of ${amount_diff} is close to Stripe fee of ${payout.fee_amount} (diff from fee: ${fee_diff}).",
        )

    # FX / rounding: small absolute difference
    if Decimal("0") < amount_diff <= _ROUNDING_THRESHOLD:
        return (
            "fx_rounding",
            f"Small rounding/FX difference of ${amount_diff}.",
        )

    # Timing: amount matches but dates differ
    if amount_diff <= _ROUNDING_THRESHOLD and day_diff > 0:
        return (
            "timing",
            f"Amount matches within tolerance but deposit is {day_diff} day(s) "
            f"{'after' if day_diff > 0 else 'before'} payout arrival.",
        )

    # Pure timing (zero amount diff, nonzero day diff)
    if amount_diff == Decimal("0") and day_diff > 0:
        return (
            "timing",
            f"Exact amount match but {day_diff} day(s) apart.",
        )

    # Large unexplained variance — classify as manual_adjustment
    return (
        "manual_adjustment",
        f"Unexplained variance of ${amount_diff} between payout net "
        f"(${payout.net_amount}) and deposit (${deposit.amount}). "
        f"Requires manual investigation.",
    )
