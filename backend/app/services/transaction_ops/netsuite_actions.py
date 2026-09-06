"""Exact NetSuite correction intents for the versioned record guard.

Preparing an intent is read-only and grants no authority to execute it. The
executor must bind the complete before/after to the current human decision,
re-read both systems, and use the durable dispatch permit exactly once.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Context, Decimal, DecimalException, localcontext
from urllib.parse import parse_qsl, urlsplit

from pydantic import ValidationError

from app.schemas.transaction_ops import TransactionSnapshot, _decimal
from app.schemas.transaction_runs import _bounded_json
from app.services.transaction_ops.inventory_identity import native_bindings
from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError, _account
from app.services.transaction_ops.normalization import NetSuiteLegacyTaxMapping
from app.services.transaction_ops.source_assessment import assessments_proven


class NetSuiteActionError(ValueError):
    """A fixed reason code suitable for a finding, never a provider response."""


@dataclass(frozen=True)
class PreparedAction:
    action: str
    _before: str
    _after: str

    @property
    def before_json(self):
        return json.loads(self._before)

    @property
    def after_json(self):
        return json.loads(self._after)


def validate_guard_url(value, account_id):
    try:
        account = _account(account_id)
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or url.netloc != f"{account}.restlets.api.netsuite.com"
            or url.path != "/app/site/hosting/restlet.nl"
            or url.fragment
            or sorted(parse_qsl(url.query, keep_blank_values=True))
            != [("deploy", "customdeploy_ecom_tx_ops_guard"), ("script", "customscript_ecom_tx_ops_guard")]
        ):
            raise ValueError
    except (ValueError, TypeError, AttributeError, NetSuiteEvidenceError):
        raise NetSuiteActionError("invalid_guard_url") from None
    return value


def _id(value):
    if isinstance(value, dict):
        value = value.get("id")
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"\d{1,30}", str(value)):
        raise NetSuiteActionError("unknown_record_identity")
    return str(value)


def _number(value):
    try:
        return _decimal(value)
    except (ValueError, DecimalException):
        raise NetSuiteActionError("unknown_amount") from None


def _text(value):
    number = _number(value)
    return format(number.normalize(), "f") if number else "0"


def _clock(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise NetSuiteActionError("unknown_record_version") from None


def _period(target, transaction_date):
    periods = target.get("periods")
    if not isinstance(periods, dict) or periods.get("complete") is not True:
        raise NetSuiteActionError("period_unavailable")
    rows = periods.get("items")
    if not isinstance(rows, list) or len(rows) != 1:
        raise NetSuiteActionError("period_unavailable")
    period = rows[0]
    if not isinstance(period, dict) or any(
        period.get(key) != "F" for key in ("closed", "alllocked", "arlocked", "aplocked", "isadjust")
    ):
        raise NetSuiteActionError("period_locked_or_unknown")
    try:
        if (
            not date.fromisoformat(period["startdate"])
            <= date.fromisoformat(transaction_date)
            <= date.fromisoformat(period["enddate"])
        ):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise NetSuiteActionError("period_date_unproven") from None
    return _id(period["id"])


def _source(source, now):
    try:
        source = TransactionSnapshot.model_validate(source)
    except (ValueError, ValidationError):
        raise NetSuiteActionError("invalid_source_evidence") from None
    if (
        source.system != "framework"
        or source.account_id != "frame.work"
        or source.record_type != "order"
        or source.status != "confirmed"
        or not re.fullmatch(r"R[0-9]{9}(?:-[A-Z0-9]+)?", source.order_reference)
        or source.amount_basis != "transaction"
        or not source.authoritative
        or not source.lines_complete
        or not source.tax_complete
        or not source.lines
        or len(source.lines) > 500
        or source.currency is None
        or source.currency_minor_unit is None
        or source.subsidiary_id is None
        or source.updated_at is None
        or source.updated_at > source.observed_at
        or not timedelta(0) <= now - source.observed_at < timedelta(minutes=15)
    ):
        raise NetSuiteActionError("source_not_actionable")
    for field in ("total", "subtotal", "tax", "shipping", "shipping_tax", "discount"):
        value = getattr(source, field)
        if value is None or value < 0:
            raise NetSuiteActionError("unknown_source_amount")
    if source.discount != 0 or source.shipping_tax != 0:
        raise NetSuiteActionError("unsupported_tax_or_discount")
    if any(
        line.net is None or line.tax is None or line.net < 0 or line.tax < 0 or line.quantity <= 0
        for line in source.lines
    ):
        raise NetSuiteActionError("unknown_source_line")
    if any(not re.fullmatch(r"line:[1-9][0-9]{0,29}", line.key) for line in source.lines):
        raise NetSuiteActionError("source_line_identity_unproven")
    if (
        sum((line.net for line in source.lines), Decimal(0)) != source.subtotal
        or sum((line.tax for line in source.lines), Decimal(0)) != source.tax
        or source.subtotal + source.tax + source.shipping != source.total
    ):
        raise NetSuiteActionError("source_amount_inconsistent")
    unit = Decimal(1).scaleb(-source.currency_minor_unit)
    final_amounts = [
        getattr(source, field) for field in ("total", "subtotal", "tax", "shipping", "shipping_tax", "discount")
    ]
    final_amounts.extend(value for line in source.lines for value in (line.net, line.tax))
    final_amounts.extend(tax.amount for tax in source.tax_details if tax.amount is not None)
    if any(value != value.quantize(unit) for value in final_amounts):
        raise NetSuiteActionError("source_currency_precision")
    if not assessments_proven(source):
        raise NetSuiteActionError("source_assessment_unproven")
    for tax in source.tax_details:
        if tax.calculation == "source_assessment":
            continue
        if (
            tax.basis is None
            or tax.rate is None
            or tax.amount is None
            or tax.rounding is None
            or tax.rate < 0
            or tax.rate > 10
        ):
            raise NetSuiteActionError("unknown_source_tax")
        expected = tax.basis * tax.rate
        if tax.included_gross_basis is not None:
            expected = tax.included_gross_basis * tax.rate / (1 + tax.included_rate_total)
        rounding = ROUND_HALF_UP if tax.rounding == "half_up" else ROUND_HALF_EVEN
        if expected.quantize(unit, rounding=rounding) != tax.amount:
            raise NetSuiteActionError("source_tax_inconsistent")
    return source


def prepare_correction(
    target,
    source,
    *,
    reference_field="tranid",
    now=None,
    legacy_tax=None,
    account_id=None,
    tax_rounding=None,
    line_identity_mode="source_line_id",
):
    """Produce only existing-line money changes; retain a full guard snapshot."""
    try:
        with localcontext(Context(prec=60, Emin=-99, Emax=99)):
            return _prepare_correction(
                target,
                source,
                reference_field,
                now or datetime.now(timezone.utc),
                legacy_tax,
                account_id,
                tax_rounding,
                line_identity_mode,
            )
    except NetSuiteActionError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, DecimalException):
        raise NetSuiteActionError("invalid_correction_evidence") from None


def _prepare_correction(target, source, reference_field, now, legacy_tax, account_id, tax_rounding, line_identity_mode):
    source = _source(source, now)
    if legacy_tax is None and any(tax.calculation == "source_assessment" for tax in source.tax_details):
        raise NetSuiteActionError("assessment_requires_native_profile")
    if line_identity_mode not in {"source_line_id", "inventory_units"}:
        raise NetSuiteActionError("line_identity_unproven")
    if not isinstance(reference_field, str) or not re.fullmatch(
        r"tranid|otherrefnum|externalid|custbody_[a-z0-9_]+", reference_field
    ):
        raise NetSuiteActionError("invalid_reference_field")
    if (
        target.get("record_type") != "salesOrder"
        or target.get("complete") is not True
        or target.get("order_reference") != source.order_reference
    ):
        raise NetSuiteActionError("target_identity_unproven")
    header, metadata = target["header"], target["currency_metadata"]
    if (
        _id(header.get("currency")) != _id(metadata.get("id"))
        or metadata.get("symbol") != source.currency
        or type(metadata.get("currencyPrecision")) is not int
        or metadata["currencyPrecision"] != source.currency_minor_unit
        or _id(header.get("subsidiary")) != source.subsidiary_id
    ):
        raise NetSuiteActionError("currency_or_subsidiary_mismatch")
    if (header.get("orderStatus") or {}).get("id") != "B":
        raise NetSuiteActionError("order_not_unfulfilled")
    if _number(header.get("handlingCost")) != 0 or _number(header.get("discountTotal")) != 0:
        raise NetSuiteActionError("unsupported_handling_or_discount")
    version = _clock(target.get("version"))
    if version != _clock(header.get("lastModifiedDate")) or version > now:
        raise NetSuiteActionError("record_version_unproven")
    before = {
        "record_id": _id(target["record_id"]),
        "reference_field": reference_field,
        "order_reference": source.order_reference,
        "version": version.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "entity": _id(header.get("entity")),
        "subsidiary": _id(header.get("subsidiary")),
        "currency": _id(header.get("currency")),
        "trandate": header["tranDate"],
        "orderstatus": "B",
    }
    before["period_id"] = _period(target, before["trandate"])
    profile = None
    if legacy_tax is not None:
        profile = NetSuiteLegacyTaxMapping.model_validate(legacy_tax)
        before.update(_legacy_before(target, source, profile, account_id))
    for api, guard in (
        ("exchangeRate", "exchangerate"),
        ("total", "total"),
        ("subtotal", "subtotal"),
        ("taxTotal", "taxtotal"),
        ("shippingCost", "shippingcost"),
        ("handlingCost", "handlingcost"),
        ("discountTotal", "discounttotal"),
        ("custbody_fw_solidus_order_total", "custbody_fw_solidus_order_total"),
    ):
        before[guard] = _text(header.get(api))
    lines = target.get("lines")
    if not isinstance(lines, list) or len(lines) != len(source.lines):
        raise NetSuiteActionError("line_membership_changed")
    bindings = {}
    if line_identity_mode == "inventory_units":
        bindings = native_bindings(lines, source)
        if not bindings:
            raise NetSuiteActionError("line_identity_unproven")
        before["line_identity_mode"] = line_identity_mode
    before["lines"], changes, seen, seen_source = [], [], set(), set()
    source_lines = {line.key: line for line in source.lines}
    used_taxes = set()
    for line in lines:
        line_id = _id(line.get("line"))
        key = bindings[line_id].key if bindings else f"line:{_id(line.get('custcol_fw_solidus_line_id'))}"
        if line_id in seen or key in seen_source or key not in source_lines:
            raise NetSuiteActionError("ambiguous_line_identity")
        seen.add(line_id)
        seen_source.add(key)
        desired = source_lines[key]
        if (
            line.get("isClosed") is not False
            or _number(line.get("quantityFulfilled")) != 0
            or _number(line.get("quantityBilled")) != 0
        ):
            raise NetSuiteActionError("fulfilled_billed_or_closed_line")
        if _number(line.get("quantity")) != desired.quantity:
            raise NetSuiteActionError("quantity_changed")
        rate = desired.net / desired.quantity
        if rate * desired.quantity != desired.net or rate.as_tuple().exponent < -12:
            raise NetSuiteActionError("unsupported_exact_rate")
        tax_id = profile.tax_code_id if profile and profile.mode == "aggregate_header" else _id(line.get("taxCode"))
        taxes = [tax for tax in source.tax_details if tax.key.startswith(f"{key}:tax:")]
        if (not profile and len(taxes) > 1) or (desired.tax != 0 and not taxes):
            raise NetSuiteActionError("unsupported_tax_allocation")
        percent = None if profile else _number(line.get("taxRate1"))
        if profile:
            if (
                tax_id != profile.tax_code_id
                or any(
                    (tax.allocation_key or tax.key) != f"{key}:tax:{tax_id}" or tax.basis != desired.net
                    for tax in taxes
                )
                or sum((tax.amount for tax in taxes), Decimal(0)) != desired.tax
            ):
                raise NetSuiteActionError("tax_code_or_allocation_changed")
            used_taxes.update(tax.key for tax in taxes)
        elif taxes:
            tax = taxes[0]
            if tax.key != f"{key}:tax:{tax_id}" or tax.amount != desired.tax:
                raise NetSuiteActionError("tax_code_or_allocation_changed")
            used_taxes.add(tax.key)
            percent = tax.rate * 100
        elif desired.tax != 0 or percent != 0:
            raise NetSuiteActionError("unknown_source_tax")
        original = {
            "line": line_id,
            "lineuniquekey": _id(line.get("lineUniqueKey")),
            "item": _id(line.get("item")),
            "isclosed": False,
        }
        if bindings:
            original["inventory_unit_ids"] = list(desired.inventory_unit_ids)
            original["custcol_fw_original_ecom_sku"] = desired.sku
        else:
            original["custcol_fw_solidus_line_id"] = key.removeprefix("line:")
        if not profile or profile.mode == "line_tax_amount":
            original["taxcode"] = tax_id
        for api, guard in (
            ("quantity", "quantity"),
            ("quantityFulfilled", "quantityfulfilled"),
            ("quantityBilled", "quantitybilled"),
            ("rate", "rate"),
            ("amount", "amount"),
            ("custcol_fw_vat_amount", "custcol_fw_vat_amount"),
        ):
            original[guard] = _text(line.get(api))
        if not profile or profile.mode == "line_tax_amount":
            original["taxrate1"] = _text(line.get("taxRate1"))
        if profile:
            if profile.mode == "aggregate_header":
                original["istaxable"] = True
            if profile.mode == "line_tax_amount":
                original["tax1amt"] = _text(line.get("tax1Amt"))
        before["lines"].append(original)
        fields = {"rate": _text(rate), "amount": _text(desired.net), "custcol_fw_vat_amount": _text(desired.tax)}
        if profile and profile.mode == "line_tax_amount":
            fields["tax1amt"] = _text(desired.tax)
        elif not profile and percent != _number(line.get("taxRate1")):
            fields["taxrate1"] = _text(percent)
        if any(original[field] != value for field, value in fields.items()):
            changes.append({"line": line_id, "fields": fields})
    if any(tax.amount != 0 and tax.key not in used_taxes for tax in source.tax_details):
        raise NetSuiteActionError("unsupported_tax_allocation")
    body = {}
    for field, value in (("custbody_fw_solidus_order_total", source.total), ("shippingcost", source.shipping)):
        if _number(before[field]) != value:
            body[field] = _text(value)
    if profile:
        if _number(before["custbody_fw_solidus_tax_amount"]) != source.tax:
            body["custbody_fw_solidus_tax_amount"] = _text(source.tax)
        if profile.mode == "aggregate_header":
            if tax_rounding not in {"half_up", "half_even"}:
                raise NetSuiteActionError("native_tax_rounding_unproven")
            # All native lines are explicitly taxable and shipping is zero.
            # The native basis is their net subtotal, even when source prices
            # include VAT. The import's currency-dependent approximation is
            # not sufficient evidence of NetSuite's taxable basis.
            basis = source.subtotal
            if source.tax and basis <= 0:
                raise NetSuiteActionError("unsupported_header_tax_basis")
            rate = (
                (source.tax * 100 / basis).quantize(Decimal("0.0000001"), rounding=ROUND_HALF_UP)
                if basis
                else Decimal(0)
            )
            if rate > 1000 or (source.tax > 0 and rate == 0):
                raise NetSuiteActionError("unsupported_header_tax_rate")
            unit = Decimal(1).scaleb(-source.currency_minor_unit)
            rounding = ROUND_HALF_UP if tax_rounding == "half_up" else ROUND_HALF_EVEN
            if (basis * rate / 100).quantize(unit, rounding=rounding) != source.tax:
                raise NetSuiteActionError("unsupported_header_tax_precision")
            if _number(before["taxrate"]) != rate:
                body["taxrate"] = _text(rate)
    if not changes and not body:
        raise NetSuiteActionError("no_change")
    after = {
        "line_changes": changes,
        "body_changes": body,
        "expected_totals": {
            "total": _text(source.total),
            "subtotal": _text(source.subtotal),
            "taxtotal": _text(source.tax),
            "shippingcost": _text(source.shipping),
            "discounttotal": "0",
        },
    }
    return PreparedAction("correct_amounts", json.dumps(_bounded_json(before)), json.dumps(_bounded_json(after)))


def _legacy_before(target, source, profile, account_id):
    if _account(account_id) != profile.account_id or source.subsidiary_id != profile.subsidiary_id:
        raise NetSuiteActionError("legacy_tax_scope_mismatch")
    if target.get("tax_details", "missing") is not None:
        raise NetSuiteActionError("legacy_tax_record_required")
    header, lines = target["header"], target["lines"]
    native_tax = _number(header.get("taxTotal"))
    if (
        native_tax < 0
        or _number(header.get("custbody_fw_solidus_tax_amount")) != native_tax
        or sum((_number(line.get("custcol_fw_vat_amount")) for line in lines), Decimal(0)) != native_tax
        or any(_number(line.get("custcol_fw_vat_amount")) < 0 for line in lines)
        or (profile.mode == "aggregate_header" and any(line.get("isTaxable") is not True for line in lines))
    ):
        raise NetSuiteActionError("legacy_tax_allocation_unproven")
    before = {
        "tax_profile": {"mode": profile.mode, "tax_code_id": profile.tax_code_id},
        "custbody_fw_solidus_tax_amount": _text(native_tax),
    }
    for api, field in (("shippingTax1Rate", "shippingtax1rate"), ("shippingTax2Rate", "shippingtax2rate")):
        before[field] = _text(header[api]) if header.get(api) is not None else None
        if (source.shipping or _number(header.get("shippingCost"))) and before[field] != "0":
            raise NetSuiteActionError("unsupported_shipping_tax")
    if profile.mode == "aggregate_header":
        if source.shipping != 0 or _number(header.get("shippingCost")) != 0:
            raise NetSuiteActionError("unsupported_aggregate_shipping")
        if _id(header.get("taxItem")) != profile.tax_code_id or header.get("isTaxable") is not True:
            raise NetSuiteActionError("legacy_tax_code_changed")
        rate = _number(header.get("taxRate"))
        if not 0 <= rate <= 1000:
            raise NetSuiteActionError("legacy_tax_rate_unproven")
        before.update(taxitem=profile.tax_code_id, taxrate=_text(rate), istaxable=True)
    elif any(_number(line.get("tax1Amt")) != _number(line.get("custcol_fw_vat_amount")) for line in lines):
        raise NetSuiteActionError("legacy_tax_allocation_unproven")
    return before
