"""Exact order, tax and completed-refund comparisons; no financial mutations.

Order totals already include tax. These three dimensions must remain separate,
not summed into an invented overall variance. Missing evidence is never zero.
"""

import re
from decimal import Decimal, DecimalException, localcontext

from app.schemas.transaction_ops import _decimal
from app.services.transaction_ops.netsuite_reader import _account, _id
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


def _signed_amount(value, precision):
    """An exact amount at the currency precision that may be negative, or None."""
    try:
        amount = _decimal(value)
        return amount if amount == amount.quantize(Decimal(1).scaleb(-precision)) else None
    except (ValueError, TypeError, DecimalException):
        return None


def _refund(evidence, reference, currency, precision):
    if not isinstance(evidence, dict) or evidence.get("complete") is not True:
        return None
    if evidence.get("order_reference") != reference or evidence.get("currency") != currency:
        return None
    return _amount(evidence.get("amount"), precision)


def _counted(credits, precision):
    """A complete read's credits and their sums, or None when any is unproven.

    The stated sums must equal their credits, and every credit must be proven a sales
    reduction by its own lines; one that is not -- a tax-account or expense posting --
    leaves the difference unexplained (packet review 2026-10-16, F2).
    """
    if not isinstance(credits, dict) or credits.get("complete") is not True:
        return None
    rows = credits.get("credits")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return None
    amounts = [(_amount(row.get("total"), precision), _amount(row.get("tax"), precision)) for row in rows]
    if any(total is None or tax is None or total <= 0 for total, tax in amounts):
        return None
    total, tax = sum((t for t, _ in amounts), Decimal(0)), sum((x for _, x in amounts), Decimal(0))
    if _amount(credits.get("total"), precision) != total or _amount(credits.get("tax"), precision) != tax:
        return None
    if any(row.get("posting") != "income" for row in rows):
        return None
    return {"rows": rows, "total": total, "tax": tax}


def _invoice_credits(evidence, order_id, reference, currency, precision):
    """Credit memos created from this order's invoice, and apart from them those created
    from its own return authorization (C15), from the same NetSuite refund read.

    Only a complete read of the same order, reference and currency counts, and the
    stated sums must equal their credits. Anything else is unknown, never zero. Return
    credits are None unless every one is proven; they never make the invoice credits unknown.
    """
    if _refund(evidence, reference, currency, precision) is None:
        return None
    manifest, credits = evidence.get("dependency_manifest"), evidence.get("invoice_credits")
    if not isinstance(manifest, dict) or not isinstance(credits, dict):
        return None
    if str(manifest.get("order_id")) != str(order_id):
        return None
    invoice = _counted(credits, precision)
    if invoice is None:
        return None
    returns = _counted(credits.get("return_credits"), precision)
    if not invoice["rows"] and not (returns and returns["rows"]):
        return None
    # The order's invoices, read in the same call. Credits are subtracted from the sales
    # order, so they explain it only when the invoices equal it (packet review 2026-10-16, F1).
    invoices = credits.get("invoices")
    if not isinstance(invoices, dict) or not invoices.get("ids"):
        return None
    invoiced = (_amount(invoices.get("total"), precision), _amount(invoices.get("tax"), precision))
    if None in invoiced:
        return None
    return {**invoice, "returns": returns if returns and returns["rows"] else None, "invoiced": invoiced}


# The fields an applied credit is shown with; a return credit has no invoice_id.
_CREDIT_FIELDS = ("id", "number", "origin_id", "origin_type", "invoice_id", "total", "tax")


def reconcile_order(source_evidence, target_evidence, config, *, refunds=None):
    from app.services.transaction_ops.source_eligibility import exclusion_report, payment_failed

    if len(source_evidence.get("orders") or []) == 1 and payment_failed(source_evidence["orders"][0]):
        return exclusion_report(source_evidence)["balance"]
    with localcontext() as context:
        context.prec = 60
        result = _reconcile(source_evidence, target_evidence, config, refunds or {})
    return _open_return_authorizations(result, (refunds or {}).get("target"))


OPEN_RETURN_AUTHORIZATION = "return_authorization_open_after_manual_refund"


def _open_return_authorizations(result, evidence):
    """Name the open return authorizations behind hand-booked refunds, and keep the order in review (C09, 2026-10-25).

    The NetSuite read leaves out a hand-booked refund credit whose return authorization is still open,
    since that authorization could still pay the customer a second time. Left out, its money is not
    seen: a source refund that already equals what was counted -- none at all, or one NetSuite paid
    twice -- would match the order and hide the refund (review C09-R1). So the order's own complete read
    names them on the result whatever its status, and held_by_open_return_authorization keeps it from
    matching; an authorization whose id cannot be read still holds it. The amounts are unchanged.
    """
    if (
        not isinstance(evidence, dict)
        or evidence.get("complete") is not True
        or evidence.get("order_reference") != result.get("order_reference")
        or evidence.get("currency") != result.get("currency")
        or not isinstance(evidence.get("tagged_credits_rejected"), list)
    ):
        return result
    held = [
        entry
        for entry in evidence["tagged_credits_rejected"]
        if isinstance(entry, dict) and entry.get("reason") == "return_authorization_open"
    ]
    if held:
        result["open_return_authorizations"] = sorted(
            {authorization for entry in held if (authorization := _id(entry.get("return_authorization_id")))},
            key=int,
        )
    return held_by_open_return_authorization(result)


def held_by_open_return_authorization(balance):
    """A balance naming an open return authorization is a difference, never matched (C09-R1, 2026-10-25).

    The reconciliation and every later step that labels the balance again (the posting recheck after a
    correction, accounting_credit_recheck.project) pass through here, and case_service._cleared never
    reconciles a case on such a balance. An incomplete balance keeps its status: it is not matched.
    """
    if "open_return_authorizations" in balance and balance.get("status") in {"matched", "difference"}:
        balance.update(status="difference", reason=OPEN_RETURN_AUTHORIZATION)
    return balance


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
    settled = _canceled_order_settled(source, header, refunds.get("target"), reference, currency, precision)
    if settled:
        # Nothing is owed on either side; the refunds still have to agree exactly.
        values = {**values, "order_total": (Decimal(0), Decimal(0)), "tax": (Decimal(0), Decimal(0))}
        return _finish(result, values, original_values, [settled], precision)
    adjustments = []
    native_zero = _native_zero_tax(tax, target, header, precision)
    if native_zero:
        # original_amounts keeps the header's own tax as unread; only the comparison uses the zero.
        adjustments.append(native_zero)
        values["tax"] = (values["tax"][0], Decimal(0))
        result["target_tax_basis"] = "native_taxtotal"
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
            elif key == "tax" and _over_reversal_within_rounding(values, adjustment, credits, config, precision):
                values[key] = (left, right - adjustment)
        if values != original_values:
            adjustments.extend(credits)
    if not credits:
        from app.services.transaction_ops.commercial_credits import verified_commercial_adjustment

        commercial = verified_commercial_adjustment(source, target_evidence, config)
        if commercial:
            left, right = values["order_total"]
            amount = _amount(
                commercial["discount_amount"]
                if commercial["kind"] == "posted_invoice_discount"
                else commercial["credit_amount"],
                precision,
            )
            if left is not None and right is not None and amount is not None and right - amount == left:
                # A posting adjustment reconciles the invoice, not the source
                # sales-order record. Keep its outstanding variance visible.
                result["posting_reconciliation"] = {
                    "status": "matched",
                    "source": f"{left:.{precision}f}",
                    "net_posting_total": f"{right - amount:.{precision}f}",
                    "delta": f"{Decimal(0):.{precision}f}",
                    "basis": commercial["kind"],
                    "sales_order_alignment": "required",
                }
                adjustments.append(commercial)
    # Framework books a Solidus order adjustment as a credit memo created from the
    # invoice and leaves the sales order as it was (decided 2026-09-30, and on 10-01:
    # such an order is matched, not left for a sales-order amendment). The credits
    # count only when they explain the difference exactly, total and tax together.
    # An order that already matches keeps its plain comparison, so return credits
    # booked against the invoice never create a difference.
    # They count only on an order with no refund tax adjustment at all, read or applied
    # (decided 2026-10-16): combining the two proofs let one credit be partly accounted
    # for, or counted from two different reads (packet review rounds 2-4, F3-F6). None of
    # Framework's 35 credit-explained open cases has one; a mixed order stays for a person.
    # C15: a returned order is credited from its own return authorization, which the
    # invoice credits never include. Two tiers, never added together by default: the
    # invoice credits alone first, exactly as before; only when they do not explain the
    # difference, the invoice credits with every one of the order's return credits. Solidus
    # does not lower its total for a return, so adding them first would break a match the
    # invoice credits make. Neither tier is ever applied in part.
    tax_adjustment_read = bool((refunds.get("target") or {}).get("tax_adjustments"))
    invoice = (
        None
        if credits or tax_adjustment_read
        else _invoice_credits(refunds.get("target"), header.get("id"), reference, currency, precision)
    )
    (source_total, target_total), (source_tax, target_tax) = values["order_total"], values["tax"]
    known = None not in (source_total, target_total, source_tax, target_tax)
    # The invoices are compared with what the order billed: its tax as read, or the zero NetSuite's own
    # figures proved when REST left the tax out (C24; review R4).
    billed = (original_values["order_total"][1], Decimal(0) if native_zero else original_values["tax"][1])
    if invoice and known and source_total != target_total and invoice["invoiced"] == billed:
        tiers = []
        if invoice["rows"]:
            tiers.append(("credit memos created from the order's invoice", invoice))
        if invoice["returns"]:
            returns = invoice["returns"]
            tiers.append(
                (
                    "credit memos created from the order's invoice or its own return authorization",
                    {
                        "rows": [*invoice["rows"], *returns["rows"]],
                        "total": invoice["total"] + returns["total"],
                        "tax": invoice["tax"] + returns["tax"],
                    },
                )
            )
        for basis, tier in tiers:
            credited = {
                **values,
                "order_total": (source_total, target_total - tier["total"]),
                "tax": (source_tax, target_tax - tier["tax"]),
            }
            if credited["order_total"][1] != source_total:
                continue
            # C15-R1: Solidus keeps the pre-adjustment tax on an order adjusted to zero, so a credit
            # landing on a zero source total is never compared with that tax: it counts only when the
            # zero-order rule holds on the credited values (no NetSuite tax left, no money taken).
            if source_total == 0 and source_tax != 0:
                if not _zero_total_order_tax(source, credited, precision):
                    continue
            elif credited["tax"][1] != source_tax:
                continue
            values["order_total"], values["tax"] = credited["order_total"], credited["tax"]
            # The order itself is matched: the 2026-09-13 note that the sales order
            # still needs a source-backed amendment no longer applies (decided 10-01).
            result.pop("posting_reconciliation", None)
            adjustments.append(
                {
                    "kind": "invoice_credit_memos",
                    "status": "verified",
                    "basis": basis,
                    "credits": [{key: row[key] for key in _CREDIT_FIELDS if key in row} for row in tier["rows"]],
                    "total": f"{tier['total']:.{precision}f}",
                    "tax": f"{tier['tax']:.{precision}f}",
                }
            )
            break
    if (
        not credits
        and not tax_adjustment_read
        and "posting_reconciliation" not in result
        and not any(adjustment.get("kind") == "invoice_credit_memos" for adjustment in adjustments)
    ):
        billed = _billed_invoices(header, refunds.get("target"), values, reference, currency, precision)
        if billed:
            if billed.pop("sales_order_alignment"):
                result["posting_reconciliation"] = {
                    "status": "matched",
                    "source": billed["total"],
                    "net_posting_total": billed["total"],
                    "delta": f"{Decimal(0):.{precision}f}",
                    "basis": billed["kind"],
                    "sales_order_alignment": "required",
                }
            else:
                values["order_total"] = (values["order_total"][0], _amount(billed["total"], precision))
                values["tax"] = (values["tax"][0], _amount(billed["tax"], precision))
            adjustments.append(billed)
    zero = _zero_total_order_tax(source, values, precision)
    if zero:
        adjustments.append(zero)
        values["tax"] = (values["tax"][1], values["tax"][1])
    rounding = _within_rounding(values, config, precision)
    if rounding:
        adjustments.append(rounding)
        for key in ("order_total", "tax"):
            values[key] = (values[key][0], values[key][0])
    return _finish(result, values, original_values, adjustments, precision, rounding)


def _finish(result, values, original_values, adjustments, precision, rounding=None):
    """The compared amounts, the adjustments that explain them, and the status."""
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
        result.update(
            status="matched",
            reason="within_rounding_tolerance"
            if rounding
            else "verified_adjustments_agree"
            if adjustments
            else "all_amounts_agree",
        )
    return result


DEPOSIT_FULLY_APPLIED = "C"


def _canceled_order_settled(source, header, evidence, reference, currency, precision):
    """A canceled order NetSuite has settled, or None (decided by Aiden, 2026-10-22).

    Canceled in the source with nothing still paid there, the sales order closed or cancelled, nothing
    billed (no invoice or cash sale) and every customer deposit fully applied: refunded, or moved to
    another order. The closed sales order's header total is then not an amount anyone owes. Each fact
    must be read, never assumed: unread billing or deposits leave the plain comparison.
    """
    if source.get("state") != "canceled" or _amount(source.get("payment_total"), precision) != 0:
        return None
    from app.services.transaction_ops.netsuite_refunds import CLOSED_ORDER_STATUSES, order_status

    status = order_status(header)
    if status not in CLOSED_ORDER_STATUSES or _refund(evidence, reference, currency, precision) is None:
        return None
    billing, deposits = evidence.get("billing_ids"), evidence.get("deposits")
    if not isinstance(billing, list) or billing or not isinstance(deposits, list):
        return None
    read = []
    for deposit in deposits:
        total = _amount(deposit.get("total"), precision) if isinstance(deposit, dict) else None
        if total is None or deposit.get("status") != DEPOSIT_FULLY_APPLIED or not deposit.get("id"):
            return None
        read.append({"id": str(deposit["id"]), "total": f"{total:.{precision}f}", "status": DEPOSIT_FULLY_APPLIED})
    return {
        "kind": "canceled_order_settled",
        "status": "verified",
        "basis": (
            "canceled in the source; the sales order is closed with nothing billed and every deposit fully applied"
        ),
        "sales_order_status": status,
        "deposits": read,
    }


# Header amounts that count as zero when the record API leaves them out (the summary money
# fields of commercial_credits). The discount is signed, negative in REST, and added as it is.
_ABSENT_IS_ZERO = ("discountTotal", "shippingCost", "handlingCost")


def _native_zero_tax(source_tax, target, header, precision):
    """The sales order's tax proven zero when the record API left out taxTotal, or None (C24, 2026-10-25).

    The record API drops an empty summary money field, so a tax-free order can arrive with no
    taxTotal. Absence is not zero, so it is proven, as for a credit memo's absent taxTotal in
    refund_adjustments (where present but null is malformed evidence, not a zero): the header's
    own arithmetic must leave no room for tax, and NetSuite's SuiteQL figures for the same record
    must give zero tax at the header's total. Those come from the identity read just before the
    record, not the same observation, so neither proof is enough alone. Only for a source that
    charged no tax: a taxed source keeps its tax missing rather than a difference against a zero
    no record showed.
    """
    if source_tax != 0 or "taxTotal" in header:
        return None
    native, record_id = target.get("native_header"), _id(header.get("id"))
    if not isinstance(native, dict) or record_id is None or _id(native.get("id")) != record_id:
        return None
    total, subtotal = _amount(header.get("total"), precision), _amount(header.get("subtotal"), precision)
    native_tax, native_total = _amount(native.get("taxtotal"), precision), _amount(native.get("total"), precision)
    if None in (total, subtotal, native_tax, native_total) or native_tax != 0 or native_total != total:
        return None
    parts = {}
    for key in _ABSENT_IS_ZERO:
        if key in header:
            parts[key] = (_signed_amount if key == "discountTotal" else _amount)(header[key], precision)
            if parts[key] is None:
                return None
        else:
            parts[key] = None
    if total != subtotal + sum((amount for amount in parts.values() if amount is not None), Decimal(0)):
        return None

    def text(amount):
        return f"{amount:.{precision}f}" if amount is not None else None

    return {
        "kind": "native_zero_tax",
        "status": "verified",
        "basis": (
            "the record API left out taxTotal; NetSuite's SuiteQL tax total for the same record is zero at "
            "the same total, and the header's own arithmetic leaves no room for tax"
        ),
        "record_id": record_id,
        "native_taxtotal": text(native_tax),
        "native_total": text(native_total),
        # As the header reported them; None is a field the record API left out, counted as zero.
        "arithmetic": {"total": text(total), "subtotal": text(subtotal), **{k: text(v) for k, v in parts.items()}},
    }


BILLED = "G"  # the sales order is billed in full: nothing is left to invoice


def _billed_invoices(header, evidence, values, reference, currency, precision):
    """The order's posted invoices when they equal the source and its billed sales order does not (C14b).

    order_reconciliation compares the sales order header, which does not post. Billed in full, the order
    owes what its invoices say; once they equal the source in total and tax, it is matched on them and
    the header stays in original_amounts. Only with nothing else in play: the complete refund read of this
    order names exactly these invoices, and no invoice, return or hand-booked credit and no tax
    adjustment, each read and not assumed. An order whose invoices total zero, like the source, leaves
    the source's tax to _zero_total_order_tax. The shape the sales-order alignment write fixes (one
    invoice, no tax anywhere, at exchange rate 1, the header above it) keeps its difference with that
    write required (decided by Aiden, 2026-10-22). Anything else returns None.
    """
    from app.services.transaction_ops.netsuite_refunds import order_status

    if order_status(header) != BILLED or _refund(evidence, reference, currency, precision) is None:
        return None
    if evidence.get("tax_adjustments") or evidence.get("tagged_credits") != []:
        return None
    credits = evidence.get("invoice_credits")
    returns = credits.get("return_credits") if isinstance(credits, dict) else None
    for read in (credits, returns):
        if not isinstance(read, dict) or read.get("complete") is not True or read.get("credits") != []:
            return None
    invoices = evidence.get("billed_invoices")
    if not isinstance(invoices, dict) or invoices.get("complete") is not True:
        return None
    ids, rates = invoices.get("ids"), invoices.get("exchange_rates")
    if not isinstance(ids, list) or not ids or ids != evidence.get("billing_ids") or not isinstance(rates, list):
        return None
    total, tax = _amount(invoices.get("total"), precision), _amount(invoices.get("tax"), precision)
    (source_total, target_total), (source_tax, target_tax) = values["order_total"], values["tax"]
    if None in (total, tax, source_total, source_tax, target_total):
        return None
    if (source_total, source_tax) == (target_total, target_tax) or total != source_total:
        return None
    if tax != source_tax and not (total == 0 and tax == 0):
        return None
    try:
        # The sales order's own rate as well: the alignment write refuses any other (review R2).
        base_rate = (
            bool(rates)
            and all(_decimal(rate) == 1 for rate in rates)
            and ("exchangeRate" not in header or _decimal(str(header["exchangeRate"])) == 1)
        )
    except (ValueError, TypeError, DecimalException):
        return None
    alignment = (
        len(ids) == 1 and base_rate and source_tax == 0 and tax == 0 and target_tax == 0 and target_total > total
    )
    return {
        "kind": "billed_invoices_match_source",
        "status": "verified",
        "basis": (
            "the order is billed in full and its posted invoices equal the source; the sales order was not updated"
        ),
        "invoices": list(ids),
        "total": f"{total:.{precision}f}",
        "tax": f"{tax:.{precision}f}",
        "sales_order_alignment": alignment,
    }


# Payments that never moved money.
_UNPAID_PAYMENT_STATES = frozenset({"void", "failed", "invalid", "checkout"})


def _zero_total_order_tax(source, values, precision):
    """The source's tax on an order both sides total at zero, or None (decided by Aiden, 2026-10-22).

    Solidus keeps the pre-adjustment tax on an order adjusted to zero; NetSuite carries none. With
    both totals zero, no NetSuite tax, and no money taken in the source (no payment, no store credit),
    there is nothing collected to remit, so the source tax is not compared. Money taken on a zero
    order keeps the comparison, as the service-order rule before it required. Any other order keeps
    its tax comparison.
    """
    (source_total, target_total), (source_tax, target_tax) = values["order_total"], values["tax"]
    if source_total != 0 or target_total != 0 or target_tax != 0 or source_tax is None or source_tax == 0:
        return None
    for key in ("payment_total", "total_applicable_store_credit"):
        if key in source and _amount(source.get(key), precision) != 0:
            return None
    for payment in source.get("payments") or []:
        try:
            amount = _decimal(payment.get("amount")) if isinstance(payment, dict) else None
        except (ValueError, TypeError, DecimalException):
            return None
        if amount is None or (amount != 0 and payment.get("state") not in _UNPAID_PAYMENT_STATES):
            return None
    return {
        "kind": "zero_total_order_tax",
        "status": "verified",
        "basis": "the source and NetSuite order totals are both zero, so the source's tax is not owed",
        "source_tax": f"{source_tax:.{precision}f}",
    }


def _over_reversal_within_rounding(values, adjustment, credits, config, precision):
    """Whether verified VAT reversals may exceed the sales order's tax by a configured rounding amount (C06).

    NetSuite can compute the VAT a cent below what Solidus removed and the reversal refunded, so the
    reversal is larger than the header tax it reverses. Only tax-reversal credits, and only when the
    order then sits within the config's tolerance as _within_rounding will require: the excess, what is
    left of the tax and the already adjusted order total all within it, and refunds that agree exactly.
    _within_rounding then records the cent. Otherwise nothing changes, so a real over-reversal stays a
    difference and never shows a negative NetSuite tax.
    """
    from app.services.transaction_ops.rounding_tolerance import configured_tolerance

    allowed = configured_tolerance(config, precision)
    (left, right), (total_left, total_right), (refunds_left, refunds_right) = (
        values["tax"],
        values["order_total"],
        values["refunds"],
    )
    if allowed is None or None in (left, right, total_left, total_right) or right >= adjustment:
        return False
    if refunds_left is None or refunds_left != refunds_right:
        return False
    if any(credit.get("kind") != "tax_reversal" for credit in credits):
        return False
    tolerance = allowed[1]
    return (
        adjustment - right <= tolerance
        and abs(left - (right - adjustment)) <= tolerance
        and abs(total_left - total_right) <= tolerance
    )


def _within_rounding(values, config, precision):
    """The recorded rounding adjustment when a configured tolerance covers what is left.

    Both the order total and the tax must be within it, and refunds must agree exactly.
    """
    from app.services.transaction_ops.rounding_tolerance import configured_tolerance

    allowed = configured_tolerance(config, precision)
    if allowed is None or any(left is None or right is None for left, right in values.values()):
        return None
    units, amount = allowed
    (source_total, target_total), (source_tax, target_tax), (source_refunds, target_refunds) = (
        values[key] for key in _METRICS
    )
    total_delta, tax_delta = source_total - target_total, source_tax - target_tax
    if (
        source_refunds != target_refunds
        or (total_delta == 0 and tax_delta == 0)
        or abs(total_delta) > amount
        or abs(tax_delta) > amount
    ):
        return None
    return {
        "kind": "rounding_tolerance",
        "minor_units": units,
        "tolerance": f"{amount:.{precision}f}",
        "order_total_delta": f"{total_delta:.{precision}f}",
        "tax_delta": f"{tax_delta:.{precision}f}",
    }
