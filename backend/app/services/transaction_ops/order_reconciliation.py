"""Exact order, tax and completed-refund comparisons; no financial mutations.

Order totals already include tax. These three dimensions must remain separate,
not summed into an invented overall variance. Missing evidence is never zero.
"""

import re
from decimal import Decimal, DecimalException, localcontext

from app.schemas.transaction_ops import _decimal
from app.services.transaction_ops.netsuite_reader import _account
from app.services.transaction_ops.normalization import source_entity_key
from app.services.transaction_ops.refund_adjustments import verified_tax_adjustments
from app.services.transaction_ops.service_order_reconciliation import service_order_offset

_METRICS = ("order_total", "tax", "refunds")


def _amount(value, precision):
    try:
        amount = _decimal(value)
        quantum = Decimal(1).scaleb(-precision)
        if amount < 0 or amount != amount.quantize(quantum):
            return None
        return amount
    except (ValueError, TypeError, DecimalException):
        return None


def _refund(evidence, reference, currency, precision):
    if not isinstance(evidence, dict) or evidence.get("complete") is not True:
        return None
    if evidence.get("order_reference") != reference or evidence.get("currency") != currency:
        return None
    return _amount(evidence.get("amount"), precision)


def reconcile_order(source_evidence, target_evidence, config, *, refunds=None):
    with localcontext() as context:
        context.prec = 60
        return _reconcile(source_evidence, target_evidence, config, refunds or {})


def _reconcile(source_evidence, target_evidence, config, refunds):
    source_orders = source_evidence.get("orders") or []
    source = source_orders[0] if len(source_orders) == 1 else {}
    reference, currency = source.get("number"), source.get("currency")
    result = {
        "order_reference": reference,
        "currency": currency,
        "target_currency": None,
        "status": "incomplete",
        "amounts": {key: {"source": None, "target": None, "delta": None} for key in _METRICS},
        "missing_metrics": list(_METRICS),
        "reason": "identity_or_coverage_unverified",
        "source_observed_at": source_evidence.get("read_at"),
        "target_observed_at": target_evidence.get("observed_at"),
    }
    if (
        source_evidence.get("source") != "framework"
        or target_evidence.get("provider") != "netsuite"
        or source_evidence.get("scope") != "order"
        or source_evidence.get("page_complete") is not True
        or not isinstance(reference, str)
        or not isinstance(currency, str)
        or not re.fullmatch(r"[A-Z]{3}", currency)
    ):
        return result
    scope = target_evidence.get("scope") or {}
    try:
        if _account(scope.get("account_id")) != _account(config["netsuite_account_id"]):
            return result
    except (ValueError, KeyError, TypeError):
        return result
    subsidiary = str(config["subsidiary_id"])
    mappings = config.get("mapping_json", {}).get("business_entity_subsidiaries", {})
    if scope.get("subsidiary_id") != subsidiary or mappings.get(source_entity_key(source)) != subsidiary:
        return result
    lookup = target_evidence.get("lookup") or {}
    targets = target_evidence.get("orders") or []
    if lookup.get("complete") is not True or lookup.get("count") != len(targets):
        return result
    if not targets:
        result.update(status="missing_in_netsuite", reason="exact_order_not_found")
        return result
    if len(targets) > 1:
        result.update(status="ambiguous", reason="multiple_exact_order_matches")
        return result
    target = targets[0]
    header, metadata = target.get("header") or {}, target.get("currency_metadata") or {}
    if (
        target.get("header_complete", target.get("complete")) is not True
        or target.get("order_reference") != reference
        or str((header.get("subsidiary") or {}).get("id")) != subsidiary
        or str((header.get("currency") or {}).get("id")) != str(metadata.get("id"))
    ):
        return result
    result["target_currency"] = metadata.get("symbol")
    if metadata.get("symbol") != currency:
        result.update(status="currency_mismatch", reason="currencies_differ")
        return result
    precision = metadata.get("currencyPrecision")
    if type(precision) is not int or not 0 <= precision <= 6:
        return result
    included = _amount(source.get("included_tax_total"), precision)
    additional = _amount(source.get("additional_tax_total"), precision)
    tax = included + additional if included is not None and additional is not None else None
    if "tax_total" in source and _amount(source["tax_total"], precision) != tax:
        tax = None
    values = {
        "order_total": (_amount(source.get("total"), precision), _amount(header.get("total"), precision)),
        "tax": (tax, _amount(header.get("taxTotal"), precision)),
        "refunds": (
            _refund(refunds.get("source"), reference, currency, precision),
            _refund(refunds.get("target"), reference, currency, precision),
        ),
    }
    original_values = dict(values)
    adjustments = []
    offset = service_order_offset(source, header, tax, precision)
    if offset:
        adjustments.append(offset)
        values["tax"] = (Decimal(0), values["tax"][1])
    credits = verified_tax_adjustments(
        refunds, config, header.get("id"), reference, currency, lambda v: _amount(v, precision)
    )
    if credits:
        adjustment = sum((_amount(c["amount"], precision) for c in credits), Decimal(0))
        tax_adjustment = sum((_amount(c.get("tax_amount", c["amount"]), precision) for c in credits), Decimal(0))
        for key, adjustment in (("order_total", adjustment), ("tax", tax_adjustment)):
            left, right = values[key]
            if left is not None and right is not None and left != right and right >= adjustment:
                values[key] = (left, right - adjustment)
        if values != original_values:
            adjustments.extend(credits)
    if adjustments:
        result["adjustments"] = adjustments
        result["original_amounts"] = {
            key: {
                "source": f"{left:.{precision}f}" if left is not None else None,
                "target": f"{right:.{precision}f}" if right is not None else None,
                "delta": f"{left - right:.{precision}f}" if left is not None and right is not None else None,
            }
            for key, (left, right) in original_values.items()
        }
    missing, differences = [], []
    for key, (left, right) in values.items():
        delta = left - right if left is not None and right is not None else None
        result["amounts"][key] = {
            "source": f"{left:.{precision}f}" if left is not None else None,
            "target": f"{right:.{precision}f}" if right is not None else None,
            "delta": f"{delta:.{precision}f}" if delta is not None else None,
        }
        if delta is None:
            missing.append(key)
        elif delta != 0:
            differences.append(key)
    result["missing_metrics"] = missing
    if differences:
        result.update(status="difference", reason="amounts_differ")
    elif missing:
        result.update(status="incomplete", reason="amount_evidence_unavailable")
    else:
        result.update(status="matched", reason="verified_adjustments_agree" if adjustments else "all_amounts_agree")
    return result
