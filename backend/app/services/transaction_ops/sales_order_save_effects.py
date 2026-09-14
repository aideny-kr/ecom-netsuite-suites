"""Verify discount-dependent save outputs without weakening approval fingerprints."""

from copy import deepcopy
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

SAVE_FIELDS = ("estGrossProfit", "estGrossProfitPercent", "custbody_esc_last_modified_date")


def _number(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Nonfinite save output")
    return number


def _profit_valid(values, total, cost):
    # Oracle gross-profit transaction fields include header discounts. This
    # adapter supports only zero tax/shipping and unchanged estimated costs.
    profit = (_number(total) - _number(cost)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if _number(total) <= 0 or _number(values["estGrossProfit"]) != profit:
        return False
    percent = (profit / _number(total) * 100).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return _number(values["estGrossProfitPercent"]) == percent


def comparison_snapshot(raw, before, account_id):
    from app.services.transaction_ops.sales_order_alignment import snapshot

    observed = snapshot(raw, amendable=True)
    previous = before.get("save_effects")
    current = observed["save_effects"]
    result = {**observed, "save_effects_valid": False}
    if previous is None:
        # Opaque legacy hashes are never silently reinterpreted.
        result["save_effects_valid"] = observed["native_digest"] == before["native_digest"]
        return result
    if set(previous) != set(current):
        return result
    try:
        profit_fields = {"estGrossProfit", "estGrossProfitPercent"}
        if any(previous.get(k) != current.get(k) for k in profit_fields):
            if not profit_fields <= current.keys() or not all(
                _profit_valid(values, total, raw["totalCostEstimate"])
                for values, total in ((previous, before["total"]), (current, raw["total"]))
            ):
                return result
        stamp = "custbody_esc_last_modified_date"
        if previous.get(stamp) != current.get(stamp):
            # Observed account-specific save stamp, not a general custom-field
            # exclusion. Other accounts retain strict equality for this field.
            if str(account_id) != "6738075":
                return result
            modified = datetime.fromisoformat(raw["lastModifiedDate"].replace("Z", "+00:00"))
            if modified.tzinfo is None:
                return result
            expected = modified.astimezone(ZoneInfo("America/Los_Angeles")).date()
            if date.fromisoformat(current[stamp]) != expected or date.fromisoformat(previous[stamp]) > expected:
                return result
        normalized = deepcopy(raw)
        normalized.update(previous)
        result["comparison_native_digest"] = snapshot(normalized, amendable=True)["native_digest"]
        result["save_effects_valid"] = True
    except (ValueError, TypeError, KeyError, InvalidOperation, ZeroDivisionError):
        pass
    return result
