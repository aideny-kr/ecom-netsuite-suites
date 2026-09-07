"""Explicit Framework/Solidus business mapping, with unknowns preserved.

Tax adjustment identities come from the API, never from parsing a display label.
Rates and rounding are versioned configuration evidence. This adapter supports
tax-only adjustments; promotional/other adjustments require a richer mapping
and remain visible without becoming a repair-ready zero discount.
"""

from collections import Counter
from datetime import datetime
from decimal import Context, Decimal, DivisionByZero, InvalidOperation, Overflow, localcontext
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.schemas.transaction_ops import (
    EvidenceModel,
    ExactDecimal,
    SourceTaxAssessment,
    TransactionLine,
    TransactionSnapshot,
    TransactionTax,
    _decimal,
)
from app.services.transaction_ops.inventory_identity import (
    native_bindings,
    native_inventory,
    sku,
    source_inventory,
    unique_ownership,
)
from app.services.transaction_ops.netsuite_reader import _account


class SourceTaxRule(EvidenceModel):
    calculation: Literal["statutory_rate", "source_assessment"] = "statutory_rate"
    rate: ExactDecimal | None = Field(default=None, ge=0, le=10)
    included: bool = Field(strict=True)
    rounding: Literal["half_up", "half_even"] | None = None
    netsuite_tax_id: str = Field(pattern=r"^[0-9]+$", max_length=30)

    @model_validator(mode="after")
    def explicit_calculation(self):
        if self.calculation == "statutory_rate" and (self.rate is None or self.rounding is None):
            raise ValueError("Statutory rules require an exact rate and rounding policy")
        if self.calculation == "source_assessment" and (self.rate is not None or self.rounding is not None):
            raise ValueError("Assessment rules must not invent a statutory rate or rounding policy")
        return self


class NetSuiteCreateSkuRule(EvidenceModel):
    netsuite_sku: str = Field(min_length=1, max_length=255)
    quantity_multiplier: int = Field(strict=True, ge=1, le=1000)

    @field_validator("netsuite_sku")
    @classmethod
    def exact_sku(cls, value):
        if sku(value) is None:
            raise ValueError("A complete exact destination SKU is required")
        return value


class NetSuiteCreateMapping(EvidenceModel):
    schema_version: Literal[1]
    external_id_prefix: str = Field(pattern=r"^[A-Za-z0-9_-]{0,50}$")
    tax_mode: Literal["legacy_tax_codes"]
    location_id: str | None = Field(default=None, pattern=r"^[0-9]{1,30}$")
    shipping_item_id: str | None = Field(default=None, pattern=r"^[0-9]{1,30}$")
    custom_form_id: str | None = Field(default=None, pattern=r"^[0-9]{1,30}$")
    terms_id: str | None = Field(default=None, pattern=r"^[0-9]{1,30}$")
    transaction_timezone: str | None = Field(default=None, max_length=100)
    inventory_mode: Literal["line_location", "cross_subsidiary"] | None = None
    sku_rules: dict[str, NetSuiteCreateSkuRule] = Field(default_factory=dict, max_length=500)
    stock_location_ids: dict[str, str] = Field(default_factory=dict, max_length=100)
    inventory_subsidiary_ids: dict[str, str] = Field(default_factory=dict, max_length=100)
    shipping_method_ids: dict[str, str] = Field(default_factory=dict, max_length=100)

    @field_validator("sku_rules", "stock_location_ids", "inventory_subsidiary_ids", "shipping_method_ids")
    @classmethod
    def exact_mapping_keys(cls, value):
        if any(sku(key) is None for key in value):
            raise ValueError("Create mappings require exact nonempty source identities")
        return value


class NetSuiteLegacyTaxMapping(EvidenceModel):
    schema_version: Literal[1]
    mode: Literal["aggregate_header", "line_tax_amount"]
    account_id: str
    subsidiary_id: str = Field(pattern=r"^[0-9]{1,30}$")
    tax_code_id: str = Field(pattern=r"^[0-9]{1,30}$")

    @field_validator("account_id")
    @classmethod
    def canonical_account(cls, value):
        return _account(value)


class TransactionMapping(EvidenceModel):
    solidus_refund_step_id: UUID | None = None
    action_mode: Literal["detect_only", "propose_actions"] = "detect_only"
    line_identity_mode: Literal["source_line_id", "inventory_units"] = "source_line_id"
    netsuite_create: NetSuiteCreateMapping | None = None
    netsuite_legacy_tax: NetSuiteLegacyTaxMapping | None = None
    reference_field: str = Field(pattern=r"^(tranid|otherrefnum|externalid|custbody_[a-z0-9_]+)$", max_length=100)
    currency_minor_units: dict[str, Annotated[int, Field(strict=True, ge=0, le=6)]] = Field(
        default_factory=dict, max_length=100
    )
    tax_rules: dict[str, SourceTaxRule] = Field(default_factory=dict, max_length=500)
    netsuite_tax_rounding: Literal["half_up", "half_even"] | None = None
    business_entity_subsidiaries: dict[str, str] = Field(default_factory=dict, max_length=100)

    @field_validator("currency_minor_units")
    @classmethod
    def currency_codes(cls, value):
        if any(len(key) != 3 or not key.isascii() or not key.isupper() or not key.isalpha() for key in value):
            raise ValueError("Currency precision must be keyed by explicit ISO currency codes")
        return value


def source_entity_key(order):
    value = order.get("business_entity")
    if "business_entity" in order and value is None:
        return "legacy"
    if isinstance(value, dict):
        value = value.get("id")
    return str(value) if value is not None and value != "legacy" else None


def _money(obj, key):
    value = obj.get(key)
    return None if value is None else _decimal(value)


def _id(obj, key="id"):
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise ValueError("Business identity is missing or malformed")
    return str(value)


def _time(value):
    if value is None:
        return None
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("Evidence timestamps must have a timezone")
    return result


def normalize_framework_order(evidence, *, mapping: TransactionMapping, account_id: str, subsidiary_id: str):
    with localcontext(Context(prec=60, Emin=-99, Emax=99, traps=[InvalidOperation, DivisionByZero, Overflow])):
        return _normalize_framework(evidence, mapping=mapping, account_id=account_id, subsidiary_id=subsidiary_id)


def _normalize_framework(evidence, *, mapping, account_id, subsidiary_id):
    if (evidence.get("source"), evidence.get("scope"), evidence.get("page_complete")) != ("framework", "order", True):
        raise ValueError("A complete individual Framework order read is required")
    orders = evidence.get("orders")
    if not isinstance(orders, list) or len(orders) != 1:
        raise ValueError("Exactly one Framework order is required")
    order = orders[0]
    observed_at = _time(evidence["read_at"])
    raw_lines, raw_shipments = order.get("line_items"), order.get("shipments")
    if not isinstance(raw_lines, list) or not raw_lines or not isinstance(raw_shipments, list):
        raise ValueError("Detailed line and shipment evidence is required")
    identities = [_id(line) for line in raw_lines]
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate source line identities are ambiguous")
    adjustment_owners = Counter(
        str(adjustment.get("id"))
        for item in [*raw_lines, *raw_shipments]
        for adjustment in (item["adjustments"] if isinstance(item.get("adjustments"), list) else [])
        if isinstance(adjustment, dict) and adjustment.get("source_type") == "Spree::TaxRate"
    )

    tax_complete = isinstance(order.get("adjustments"), list) and not order.get("adjustments")
    simple_adjustments = tax_complete
    lines, taxes = [], []
    zero = Decimal(0)
    shipping_tax = zero
    included_sum = additional_sum = zero
    shipping_net = zero

    for kind, items in (("line", raw_lines), ("shipment", raw_shipments)):
        for item in items:
            identity = _id(item)
            key = f"{kind}:{identity}"
            if kind == "line":
                quantity = _money(item, "quantity")
                price = _money(item, "price")
                if quantity is None or price is None:
                    raise ValueError("Line price and quantity are required")
                gross = price * quantity
            else:
                gross = _money(item, "cost")
                if gross is None:
                    tax_complete = False
            adjustments = item.get("adjustments")
            amounts_known = isinstance(adjustments, list)
            if not isinstance(adjustments, list):
                tax_complete = False
                simple_adjustments = False
                adjustments = []
            local_taxes = []
            assessments = {}
            non_tax = zero
            local_known = True
            for adjustment in adjustments:
                amount = _money(adjustment, "amount")
                if amount is None:
                    local_known = False
                    amounts_known = False
                    simple_adjustments = False
                    continue
                if adjustment.get("source_type") != "Spree::TaxRate":
                    non_tax += amount
                    if amount:
                        simple_adjustments = False
                        local_known = False
                    continue
                source_tax_id = _id(adjustment, "source_id")
                rule = mapping.tax_rules.get(source_tax_id)
                owner_type = "Spree::LineItem" if kind == "line" else "Spree::Shipment"
                if (str(adjustment.get("adjustable_id")), adjustment.get("adjustable_type")) != (identity, owner_type):
                    local_known = False
                # This is the verified Framework import-hook predicate. Header
                # reconciliation below still has to prove these are all taxes.
                if adjustment.get("finalized") is not True and adjustment.get("eligible") is not True:
                    local_known = False
                if rule and "included" in adjustment and adjustment["included"] is not rule.included:
                    local_known = False
                if rule is None or amount < 0:
                    local_known = False
                adjustment_id = _id(adjustment)
                if rule and rule.calculation == "source_assessment":
                    profile = mapping.netsuite_legacy_tax
                    if (
                        not profile
                        or profile.subsidiary_id != subsidiary_id
                        or profile.tax_code_id != rule.netsuite_tax_id
                    ):
                        local_known = False
                    proof = None
                    try:
                        proof = SourceTaxAssessment(
                            source_tax_id=source_tax_id,
                            adjustment_id=adjustment_id,
                            finalized=adjustment.get("finalized"),
                            updated_at=adjustment.get("updated_at"),
                        )
                        version = _time(order.get("updated_at"))
                        if (
                            version is None
                            or not proof.updated_at <= version <= observed_at
                            or adjustment_owners[adjustment_id] != 1
                        ):
                            proof = None
                    except (ValueError, TypeError):
                        proof = None
                    assessments[adjustment_id] = proof
                    if proof is None:
                        local_known = False
                local_taxes.append((amount, source_tax_id, rule, adjustment_id))
            item_tax = sum((amount for amount, _, _, _ in local_taxes), zero)
            item_included = sum(
                (amount for amount, _, rule, _ in local_taxes if rule is not None and rule.included), zero
            )
            item_additional = sum(
                (amount for amount, _, rule, _ in local_taxes if rule is not None and not rule.included), zero
            )
            all_rules_known = all(rule is not None for _, _, rule, _ in local_taxes)
            included_rates = sum(
                (
                    rule.rate
                    for _, _, rule, _ in local_taxes
                    if rule is not None and rule.included and rule.rate is not None
                ),
                zero,
            )
            if len({rule.calculation for _, _, rule, _ in local_taxes if rule and rule.included}) > 1:
                local_known = False
            net = (
                gross - item_included
                if gross is not None and all_rules_known and amounts_known and not non_tax
                else None
            )
            if kind == "line":
                if gross is None or _money(item, "total") != gross + item_additional + non_tax:
                    local_known = False
                lines.append(
                    TransactionLine(
                        key=key,
                        quantity=quantity,
                        net=net,
                        tax=item_tax if amounts_known else None,
                        inventory_unit_ids=source_inventory(item)
                        if mapping.line_identity_mode == "inventory_units"
                        else (),
                        sku=sku((item.get("variant") or {}).get("sku"))
                        if mapping.line_identity_mode == "inventory_units"
                        else None,
                    )
                )
            else:
                shipping_tax += item_tax
                if net is None:
                    shipping_net = None
                elif shipping_net is not None:
                    shipping_net += net
            tax_counts = Counter(
                rule.netsuite_tax_id if rule else f"source_rate:{source_id}" for _, source_id, rule, _ in local_taxes
            )
            for amount, source_tax_id, rule, adjustment_id in local_taxes:
                tax_key = rule.netsuite_tax_id if rule else f"source_rate:{source_tax_id}"
                allocation_key = None
                if tax_counts[tax_key] > 1 or (rule and rule.calculation == "source_assessment"):
                    profile = mapping.netsuite_legacy_tax
                    if profile and profile.subsidiary_id == subsidiary_id and profile.tax_code_id == tax_key:
                        allocation_key = f"{key}:tax:{tax_key}"
                    else:
                        local_known = False
                    if rule and rule.calculation == "source_assessment":
                        allocation_key = f"{key}:tax:{tax_key}"
                    tax_key += f":source_rate:{source_tax_id}:adjustment:{adjustment_id}"
                data = {
                    "key": f"{key}:tax:{tax_key}",
                    "allocation_key": allocation_key,
                    "basis": net,
                    "amount": amount,
                    "rate": rule.rate if rule else None,
                    "rounding": rule.rounding if rule else None,
                }
                if rule and rule.calculation == "source_assessment":
                    data.update(calculation="source_assessment", assessment=assessments.get(adjustment_id))
                if (
                    rule
                    and rule.calculation == "statutory_rate"
                    and rule.included
                    and gross is not None
                    and all_rules_known
                ):
                    data.update(included_gross_basis=gross, included_rate_total=included_rates)
                taxes.append(TransactionTax(**data))
            included_sum += item_included
            additional_sum += item_additional
            tax_complete = tax_complete and local_known and gross is not None

    header_tax = _money(order, "tax_total")
    header_included = _money(order, "included_tax_total")
    header_additional = _money(order, "additional_tax_total")
    tax_complete = tax_complete and (
        header_included == included_sum
        and header_additional == additional_sum
        and header_tax == included_sum + additional_sum
        and _money(order, "adjustment_total") == additional_sum
        and _money(order, "ship_total") == sum((_money(item, "cost") or zero for item in raw_shipments), zero)
        and _money(order, "item_total")
        == sum((_money(item, "price") * _money(item, "quantity") for item in raw_lines), zero)
    )
    state = order.get("state")
    status = "unknown"
    if state == "complete":
        status = "fulfilled" if order.get("shipment_state") == "shipped" else "confirmed"
    elif state in {"canceled", "cancelled"}:
        status = "cancelled"
    elif state == "refunded":
        status = "refunded"
    if order.get("requires_review") is not False:
        status = "unknown"
    currency = order.get("currency")
    mapped_subsidiary = mapping.business_entity_subsidiaries.get(source_entity_key(order))
    return TransactionSnapshot(
        system="framework",
        account_id=account_id,
        record_id=_id(order),
        record_type="order",
        order_reference=_id(order, "number"),
        subsidiary_id=mapped_subsidiary if mapped_subsidiary == subsidiary_id else None,
        currency=currency,
        currency_minor_unit=mapping.currency_minor_units.get(currency),
        amount_basis="transaction",
        status=status,
        updated_at=_time(order.get("updated_at")),
        observed_at=observed_at,
        authoritative=True,
        total=_money(order, "total"),
        subtotal=sum((line.net for line in lines), zero) if all(line.net is not None for line in lines) else None,
        tax=header_tax,
        shipping=shipping_net,
        shipping_tax=shipping_tax if tax_complete else None,
        discount=zero if simple_adjustments else None,
        lines=lines,
        tax_details=taxes,
        lines_complete=mapping.line_identity_mode != "inventory_units" or unique_ownership(lines),
        tax_complete=tax_complete,
    )


def normalize_netsuite_order(order, *, mapping: TransactionMapping, account_id: str, observed_at: str, source=None):
    with localcontext(Context(prec=60, Emin=-99, Emax=99, traps=[InvalidOperation, DivisionByZero, Overflow])):
        return _normalize_netsuite(
            order, mapping=mapping, account_id=account_id, observed_at=observed_at, source=source
        )


def _normalize_netsuite(order, *, mapping, account_id, observed_at, source=None):
    header = order["header"]
    currency_meta = order.get("currency_metadata") or {}
    currency = currency_meta.get("symbol")
    precision = currency_meta.get("currencyPrecision")
    if type(precision) is not int or not 0 <= precision <= 6:
        precision = None
    # ISO metadata must refer to the record's actual currency. A display name
    # such as Euro, or an exchange rate, cannot supply that missing identity.
    if str(currency_meta.get("id")) != str((header.get("currency") or {}).get("id")):
        currency, precision = None, None
    raw_lines = order.get("lines")
    lines_complete = isinstance(raw_lines, list) and bool(raw_lines)
    lines, refs, taxes = [], {}, []
    seen_keys = set()
    inventory_mode = mapping.line_identity_mode == "inventory_units"
    source_scope_matches = (
        source is not None
        and source.system == "framework"
        and source.account_id == "frame.work"
        and source.record_type == "order"
        and source.authoritative
        and source.order_reference == order.get("order_reference")
        and source.subsidiary_id == str((header.get("subsidiary") or {}).get("id"))
    )
    bindings = native_bindings(raw_lines or [], source) if inventory_mode and source_scope_matches else {}
    for line in raw_lines or []:
        source_id = line.get("custcol_fw_solidus_line_id")
        if inventory_mode:
            match = bindings.get(str(line.get("line")))
            if match is None:
                lines_complete = False
                key = f"netsuite_line:{_id(line, 'line')}"
            else:
                key = match.key
        elif isinstance(source_id, bool) or not isinstance(source_id, (str, int)) or not str(source_id).strip():
            lines_complete = False
            key = f"netsuite_line:{_id(line, 'line')}"
        else:
            key = f"line:{source_id}"
        if key in seen_keys:
            raise ValueError("Duplicate NetSuite line identities are ambiguous")
        seen_keys.add(key)
        ref = line.get("taxDetailsReference")
        if ref is not None:
            if str(ref) in refs:
                raise ValueError("Ambiguous NetSuite tax detail reference")
            refs[str(ref)] = key
        quantity = _money(line, "quantity")
        if quantity is None:
            raise ValueError("NetSuite line quantity is unknown")
        lines.append(
            TransactionLine(
                key=key,
                quantity=quantity,
                net=_money(line, "amount"),
                tax=_money(line, "custcol_fw_vat_amount"),
                inventory_unit_ids=native_inventory(line.get("custcol_fw_inventory_unit_ids"))
                if inventory_mode
                else (),
                sku=sku(line.get("custcol_fw_original_ecom_sku")) if inventory_mode else None,
            )
        )
    raw_taxes = order.get("tax_details")
    tax_complete = lines_complete and isinstance(raw_taxes, list)
    per_line = {}
    for tax in raw_taxes or []:
        key = refs.get(str(tax.get("taxDetailsReference")))
        code = (tax.get("taxCode") or {}).get("id")
        if not key or code is None:
            tax_complete = False
            continue
        amount = _money(tax, "taxAmount")
        rate = _money(tax, "taxRate")
        taxes.append(
            TransactionTax(
                key=f"{key}:tax:{code}",
                basis=_money(tax, "taxBasis"),
                rate=rate / 100 if rate is not None else None,
                amount=amount,
                rounding=mapping.netsuite_tax_rounding,
            )
        )
        if amount is None:
            tax_complete = False
        else:
            per_line[key] = per_line.get(key, Decimal(0)) + amount
    if isinstance(raw_taxes, list):
        normalized_lines = []
        for line in lines:
            actual = per_line.get(line.key)
            if actual is None:
                # Missing tax detail isn't proof of zero unless the record
                # provides a zero amount for the line independently.
                actual = line.tax if line.tax == 0 else None
            if line.tax is not None and actual is not None and line.tax != actual:
                tax_complete = False
            normalized_lines.append(line.model_copy(update={"tax": actual}))
        lines = normalized_lines
    header_tax = _money(header, "taxTotal")
    if raw_taxes is None and header_tax == 0 and lines and all(line.tax == 0 for line in lines):
        tax_complete = lines_complete
    # Legacy SOLIDUS stores an effective aggregate header rate. It must not be
    # presented as each line's statutory rate. Preserve the observed line VAT
    # and header total, pending evidence of that integration's allocation rules.
    if raw_taxes is None and mapping.netsuite_legacy_tax:
        taxes, legacy_complete = _legacy_allocations(order, lines, mapping.netsuite_legacy_tax, account_id)
        tax_complete = lines_complete and legacy_complete
    elif raw_taxes is None and header_tax:
        code = (header.get("taxItem") or {}).get("id")
        for line in lines:
            if line.tax is not None and code is not None:
                taxes.append(TransactionTax(key=f"{line.key}:tax:{code}", basis=line.net, amount=line.tax))
    line_tax_total = sum((line.tax for line in lines if line.tax is not None), Decimal(0))
    tax_complete = tax_complete and all(line.tax is not None for line in lines) and line_tax_total == header_tax
    discount = _money(header, "discountTotal")
    if discount is not None:
        discount = discount.copy_abs()
    state = (header.get("orderStatus") or header.get("status") or {}).get("id")
    status = {
        "A": "draft",
        "B": "confirmed",
        "C": "cancelled",
        "D": "confirmed",
        "E": "confirmed",
        "F": "fulfilled",
        "G": "fulfilled",
    }.get(state, "unknown")
    return TransactionSnapshot(
        system="netsuite",
        account_id=account_id,
        record_id=order["record_id"],
        record_type="salesorder",
        order_reference=order["order_reference"],
        subsidiary_id=(header.get("subsidiary") or {}).get("id"),
        currency=currency,
        currency_minor_unit=precision,
        amount_basis="transaction",
        status=status,
        updated_at=_time(order.get("version")),
        observed_at=_time(observed_at),
        authoritative=order.get("complete") is True,
        total=_money(header, "total"),
        subtotal=_money(header, "subtotal"),
        tax=header_tax,
        shipping=_money(header, "shippingCost"),
        shipping_tax=Decimal(0) if tax_complete else None,
        discount=discount,
        lines=lines,
        tax_details=taxes,
        lines_complete=lines_complete,
        tax_complete=tax_complete,
    )


def _legacy_allocations(order, lines, profile, account_id):
    """Prove native/custom allocation agreement, never reverse-engineer rates.

    These profiles describe the inspected Celigo imports. Taxed shipping is
    intentionally incomplete until a distinct shipment-to-native allocation is
    available. A native SuiteTax sublist always takes precedence over a profile.
    """
    header = order["header"]
    known = (
        _account(account_id) == profile.account_id
        and str((header.get("subsidiary") or {}).get("id")) == profile.subsidiary_id
        and _money(header, "custbody_fw_solidus_tax_amount") == _money(header, "taxTotal")
        and (
            _money(header, "shippingCost") == 0
            or (_money(header, "shippingTax1Rate") == 0 and _money(header, "shippingTax2Rate") == 0)
        )
    )
    if profile.mode == "aggregate_header":
        rate = _money(header, "taxRate")
        known = known and (
            str((header.get("taxItem") or {}).get("id")) == profile.tax_code_id
            and header.get("isTaxable") is True
            and rate is not None
            and 0 <= rate <= 1000
        )
    taxes = []
    for line, raw in zip(lines, order.get("lines") or [], strict=True):
        known = known and line.net is not None and line.net >= 0 and line.tax is not None and line.tax >= 0
        if profile.mode == "line_tax_amount":
            known = known and (
                str((raw.get("taxCode") or {}).get("id")) == profile.tax_code_id and _money(raw, "tax1Amt") == line.tax
            )
        taxes.append(
            TransactionTax(
                key=f"{line.key}:tax:{profile.tax_code_id}",
                calculation="reported_allocation",
                basis=line.net,
                amount=line.tax,
            )
        )
    return taxes, known
