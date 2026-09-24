"""Accounting evidence checks shared by the planner and both classifiers.

Only linked canonical records establish monetary facts. Search candidates and
model confidence never establish identity, currency basis, or permission to post.
"""

from decimal import Decimal, InvalidOperation


def amount(value) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def posting_context(posting) -> dict:
    return {
        key: str(getattr(posting, key)) if getattr(posting, key) is not None else None
        for key in (
            "id",
            "record_type",
            "amount",
            "currency",
            "transaction_currency",
            "foreign_amount",
            "exchange_rate",
            "memo",
            "related_payout_id",
            "subsidiary_id",
            "netsuite_internal_id",
        )
    }


def currency_basis_verified(context: dict) -> bool:
    """Cross-currency settlement needs an FX proof we do not yet produce.

    Even a tiny base-currency residual must therefore be held. Missing transaction
    currency is unknown, not an implicit assertion of base-currency settlement.
    """
    posting = context.get("matched_posting") or {}
    line = context.get("payout_line") or {}
    ref = (context.get("evidence") or {}).get("order_reference")
    currency = context.get("currency")
    subsidiary = context.get("subsidiary_id")
    return bool(
        ref
        and currency
        and subsidiary
        and posting.get("related_payout_id") == ref
        and posting.get("subsidiary_id") == subsidiary
        and posting.get("record_type", "").lower() in {"custdep", "customerdeposit"}
        and posting.get("currency") == posting.get("transaction_currency") == currency
        and amount(posting.get("amount")) is not None
        and amount(posting.get("amount")) == amount(posting.get("foreign_amount"))
        and amount(posting.get("amount")) == amount(context.get("netsuite_amount"))
        and ref in {line.get("order_reference"), line.get("related_order_id")}
        and line.get("currency") == currency
        and line.get("subsidiary_id") == subsidiary
        and line.get("line_type") == "charge"
        and amount(line.get("amount")) is not None
        and amount(line.get("amount")) == amount(context.get("stripe_amount"))
        and amount(line.get("fee")) is not None
        and amount(line.get("net")) is not None
        and amount(line.get("amount")) - amount(line.get("fee")) == amount(line.get("net"))
    )


def fee_explained(stripe, netsuite, variance, fee) -> bool:
    """Exact identity of stored monetary amounts; no absolute near-zero tolerance."""
    stripe, netsuite, variance, fee = (amount(v) for v in (stripe, netsuite, variance, fee))
    return bool(
        None not in (stripe, netsuite, variance, fee)
        and stripe > 0
        and netsuite >= 0
        and fee > 0
        and stripe - netsuite == fee == abs(variance)
    )


def action_violation(action: str, context: dict) -> str | None:
    if action in {"book_fee_line", "writeoff_je"}:
        if not currency_basis_verified(context):
            return "unverified_currency_or_linkage"
        stripe = amount(context.get("stripe_amount"))
        netsuite = amount(context.get("netsuite_amount"))
        variance = amount(context.get("variance_amount"))
        if stripe is None or netsuite is None or variance is None or abs(stripe - netsuite) != abs(variance):
            return "inconsistent_amounts"
        if action == "book_fee_line":
            line = context.get("payout_line") or {}
            if not fee_explained(stripe, netsuite, variance, line.get("fee")):
                return "unverified_fee_explanation"
            if amount(line.get("net")) != netsuite:
                return "inconsistent_fee_net"
        elif context.get("variance_type") not in {"amount_mismatch", "fx_rounding"}:
            return "unsupported_writeoff"
    if action in {"apply_deposit", "create_and_apply_deposit"}:
        # No canonical application-state/source-coverage proof producer exists yet.
        return "unverified_deposit_action"
    if action == "carry_forward":
        from app.services.reconciliation.resolution_planner import RECENT_PAYOUT_LAG_DAYS

        evidence = context.get("evidence") or {}
        if context.get("root_cause") == "washout" or str(evidence.get("washout")) == "True":
            if context.get("verified_washout") is not True:
                return "unverified_washout"
        elif context.get("variance_type") == "timing":
            if amount(context.get("stripe_amount")) is None or amount(context.get("stripe_amount")) != amount(
                context.get("netsuite_amount")
            ):
                return "unverified_timing"
        else:
            payout = context.get("payout") or {}
            days = amount(payout.get("days_since_arrival"))
            if not (
                context.get("variance_type") in {"missing", "missing_in_netsuite"}
                and days is not None
                and 0 <= days <= RECENT_PAYOUT_LAG_DAYS
                and payout.get("status") in {"paid", "pending", "in_transit"}
            ):
                return "unverified_timing"
    return None
