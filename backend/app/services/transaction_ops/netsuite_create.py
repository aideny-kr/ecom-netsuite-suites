"""Read-only missing-order inputs, with explicit routing and private proof.

An input is not a proposal or permission to write. Native preparation must still
prove absence and resolve an exact customer/currency/item/period draft before a
human can approve it. This module makes no provider calls.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Context, Decimal, DecimalException, localcontext
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.schemas.transaction_runs import _bounded_json
from app.services.transaction_ops.netsuite_actions import NetSuiteActionError, _number, _source, _text
from app.services.transaction_ops.netsuite_reader import _account
from app.services.transaction_ops.normalization import TransactionMapping, _time, normalize_framework_order
from app.services.transaction_ops.source_projection import project_order
from app.services.transaction_ops.state_service import business_digest

MAX_CREATE_LINES = 100


class CreateInputError(ValueError):
    """A fixed reason code; never include provider content or customer details."""


@dataclass(frozen=True)
class PreparedCreateInput:
    _payload: str
    source_fingerprint: str
    private_fingerprint: str

    @property
    def payload_json(self):
        return json.loads(self._payload)


def _id(value):
    if type(value) not in (str, int) or not re.fullmatch(r"[1-9][0-9]{0,29}", str(value)):
        raise CreateInputError("create_identity_unproven")
    return str(value)


def _string(value, *, empty=False, maximum=255):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not value and not empty)
        or value != value.strip()
        or any(ord(char) < 32 for char in value)
    ):
        raise CreateInputError("create_text_unproven")
    return value


def _address(raw):
    country = _string(raw["country"]["iso"])
    if not re.fullmatch(r"[A-Z]{2}", country) or raw.get("country_iso", country) != country:
        raise CreateInputError("address_country_unproven")
    state = (raw.get("state") or {}).get("abbr")
    if state is None:
        state = raw.get("state_name")
    return {
        "country": country,
        "state": _string(state, empty=True),
        "addressee": _string(raw["name"]),
        "attention": _string(raw["company"], empty=True),
        "addr1": _string(raw["address1"]),
        "addr2": _string(raw["address2"], empty=True),
        "city": _string(raw["city"]),
        "zip": _string(raw["zipcode"], empty=True),
        "addrphone": _string(raw["phone"], empty=True),
    }


def prepare_create_input(evidence, mapping, *, account_id, subsidiary_id, now=None):
    try:
        with localcontext(Context(prec=60, Emin=-99, Emax=99)):
            return _prepare(
                evidence,
                TransactionMapping.model_validate(mapping),
                _account(account_id),
                _id(subsidiary_id),
                now or datetime.now(timezone.utc),
            )
    except CreateInputError:
        raise
    except NetSuiteActionError as exc:
        raise CreateInputError(str(exc)) from None
    except (ValueError, KeyError, TypeError, AttributeError, DecimalException, ZoneInfoNotFoundError):
        raise CreateInputError("incomplete_create_evidence") from None


def _prepare(evidence, mapping, account_id, subsidiary_id, now):
    create, profile = mapping.netsuite_create, mapping.netsuite_legacy_tax
    if (
        create is None
        or create.external_id_prefix != ""
        or not create.transaction_timezone
        or create.inventory_mode is None
        or not create.sku_rules
        or not create.stock_location_ids
        or not create.shipping_method_ids
        or mapping.line_identity_mode != "inventory_units"
        or mapping.reference_field != "tranid"
        or profile is None
        or (profile.account_id, profile.subsidiary_id) != (account_id, subsidiary_id)
    ):
        raise CreateInputError("create_mapping_unproven")
    zone = ZoneInfo(create.transaction_timezone)
    source = _source(
        normalize_framework_order(evidence, mapping=mapping, account_id="frame.work", subsidiary_id=subsidiary_id),
        now,
    )
    if len(source.lines) > MAX_CREATE_LINES:
        raise CreateInputError("create_line_budget")
    order = evidence["orders"][0]
    if (
        order.get("requires_review") is not False
        or order.get("credit_sale") is not False
        or order.get("customer_type") != "consumer"
        or order.get("order_type") != "marketplace"
        or order.get("payment_state") != "paid"
        or order.get("shipment_state") != "ready"
        or _number(order.get("payment_total")) != source.total
        or _number(order.get("order_total_after_store_credit")) != source.total
        or _number(order.get("total_applicable_store_credit")) != 0
        or _number(order.get("deposit_amount")) != 0
    ):
        raise CreateInputError("unsupported_create_source_state")
    completed = _time(order.get("completed_at"))
    if completed is None or completed > source.updated_at:
        raise CreateInputError("create_date_unproven")
    payments = order.get("payments")
    if (
        not isinstance(payments, list)
        or not 1 <= len(payments) <= 100
        or any(
            payment.get("state") != "completed"
            or payment.get("source_type") != "StripeGateway::PaymentSource"
            or payment.get("currency") not in (None, source.currency)
            or (payment.get("exchange_rate") is not None and _number(payment["exchange_rate"]) != 1)
            or _number(payment.get("amount")) < 0
            for payment in payments
        )
        or len({_id(payment.get("id")) for payment in payments}) != len(payments)
        or sum((_number(payment["amount"]) for payment in payments), Decimal(0)) != source.total
    ):
        raise CreateInputError("create_payment_unproven")
    email = _string(order.get("email"), maximum=254)
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise CreateInputError("create_customer_unproven")
    shipments, methods = {}, set()
    for shipment in order["shipments"]:
        identity = _id(shipment.get("id"))
        rate = shipment.get("selected_shipping_rate") or {}
        if (
            identity in shipments
            or shipment.get("state") != "ready"
            # Individual sync embeds shipments under the proven order. Its
            # optional owner may be the full reference or numeric source ID;
            # neither partial references nor conflicting owners are accepted.
            or (
                "order_id" in shipment
                and shipment["order_id"] != source.order_reference
                and _id(shipment["order_id"]) != source.record_id
            )
            or rate.get("selected") is not True
        ):
            raise CreateInputError("create_shipment_unproven")
        stock_name = _string(shipment.get("stock_location_name"))
        inventory_subsidiary = _id(create.inventory_subsidiary_ids.get(stock_name))
        if create.inventory_mode == "line_location" and inventory_subsidiary != subsidiary_id:
            raise CreateInputError("create_inventory_scope_unproven")
        shipments[identity] = (_id(create.stock_location_ids.get(stock_name)), inventory_subsidiary)
        methods.add(_id(create.shipping_method_ids.get(_id(rate.get("shipping_method_id")))))
    if len(methods) != 1:
        raise CreateInputError("create_shipping_method_unproven")
    raw_lines = {_id(line.get("id")): line for line in order["line_items"]}
    parents, lines, allocated = {}, [], set()
    for line in source.lines:
        identity = line.key.removeprefix("line:")
        raw = raw_lines[identity]
        parent = raw["parent_id"]
        parents[identity] = None if parent is None else _id(parent)
        rule = create.sku_rules.get(line.sku)
        if rule is None:
            raise CreateInputError("create_sku_unmapped")
        if line.quantity != len(line.inventory_unit_ids):
            raise CreateInputError("create_inventory_quantity_unproven")
        locations = set()
        for unit in raw["inventory_units"]:
            if unit.get("state") != "on_hand":
                raise CreateInputError("create_inventory_state_unproven")
            locations.add(shipments[_id(unit.get("shipment_id"))])
        if len(locations) != 1:
            raise CreateInputError("create_line_location_ambiguous")
        quantity = line.quantity * rule.quantity_multiplier
        rate = line.net / quantity
        if rate * quantity != line.net or rate.as_tuple().exponent < -12:
            raise CreateInputError("unsupported_exact_rate")
        taxes = [tax for tax in source.tax_details if tax.key.startswith(f"{line.key}:tax:")]
        if (
            any(
                (tax.allocation_key or tax.key) != f"{line.key}:tax:{profile.tax_code_id}" or tax.basis != line.net
                for tax in taxes
            )
            or sum((tax.amount for tax in taxes), Decimal(0)) != line.tax
        ):
            raise CreateInputError("create_tax_allocation_unproven")
        allocated.update(tax.key for tax in taxes)
        location_id, inventory_subsidiary_id = next(iter(locations))
        lines.append(
            {
                "source_line_id": identity,
                "source_parent_id": parents[identity],
                "source_sku": line.sku,
                "netsuite_sku": rule.netsuite_sku,
                "source_quantity": _text(line.quantity),
                "quantity_multiplier": rule.quantity_multiplier,
                "quantity": _text(quantity),
                "rate": _text(rate),
                "amount": _text(line.net),
                "tax_amount": _text(line.tax),
                "tax_code_id": profile.tax_code_id,
                "inventory_unit_ids": list(line.inventory_unit_ids),
                "location_id": location_id,
                "inventory_subsidiary_id": inventory_subsidiary_id,
            }
        )
    for identity in parents:
        seen, parent = {identity}, parents[identity]
        while parent is not None:
            if parent not in parents or parent in seen:
                raise CreateInputError("create_parent_identity_unproven")
            seen.add(parent)
            parent = parents[parent]
    if any(tax.amount != 0 and tax.key not in allocated for tax in source.tax_details):
        raise CreateInputError("create_tax_allocation_unproven")
    if profile.mode == "aggregate_header" and (source.shipping != 0 or mapping.netsuite_tax_rounding is None):
        raise CreateInputError("create_aggregate_tax_policy_unproven")
    payload = {
        "schema_version": 1,
        "account_id": account_id,
        "subsidiary_id": subsidiary_id,
        "order_reference": source.order_reference,
        "external_id": source.order_reference,
        "order_status": "A",
        "customer_email": email,
        "currency": {"symbol": source.currency, "precision": source.currency_minor_unit},
        "transaction_date": completed.astimezone(zone).date().isoformat(),
        "billing_address": _address(order["bill_address"]),
        "shipping_address": _address(order["ship_address"]),
        "shipping_method_id": next(iter(methods)),
        "inventory_mode": create.inventory_mode,
        "tax_profile": profile.model_dump(mode="json"),
        "native_tax_rounding": mapping.netsuite_tax_rounding,
        "custom_form_id": create.custom_form_id,
        "terms_id": create.terms_id,
        "lines": lines,
        "expected_totals": {
            "subtotal": _text(source.subtotal),
            "taxtotal": _text(source.tax),
            "total": _text(source.total),
            "shippingcost": _text(source.shipping),
            "handlingcost": "0",
            "discounttotal": "0",
        },
    }
    return PreparedCreateInput(
        json.dumps(_bounded_json(payload)),
        business_digest(source.model_dump(mode="python", exclude={"observed_at"})),
        business_digest({"schema_version": 1, "order": project_order(order, include_sync_data=True)}),
    )


def validate_create_preview(payload, raw):
    """Accept only the exact native draft contract, retaining source-approved PII.

    Native reads establish metadata and sourcing. This independent check keeps a
    malformed or incompatible guard response out of immutable human approvals.
    """
    try:
        with localcontext(Context(prec=60, Emin=-99, Emax=99)):
            preview = _bounded_json(raw)
            _preview_keys(preview, {"schema_version", "metadata", "period_id", "record"})
            if type(preview["schema_version"]) is not int or preview["schema_version"] != 1:
                raise ValueError
            _native_id(preview["period_id"])
            meta = preview["metadata"]
            _preview_keys(meta, {"customer", "currency", "items", "locations"})
            customer, currency = meta["customer"], meta["currency"]
            _preview_keys(customer, {"id", "primary_subsidiary", "currency_ids", "credit_hold", "credit_limit"})
            _preview_keys(currency, {"id", "symbol", "precision"})
            _native_id(customer["id"])
            _native_id(customer["primary_subsidiary"])
            _native_id(currency["id"])
            currencies = customer["currency_ids"]
            if not isinstance(currencies, list) or not 1 <= len(currencies) <= 101:
                raise ValueError
            if len({_native_id(v) for v in currencies}) != len(currencies) or currency["id"] not in currencies:
                raise ValueError
            if customer["credit_hold"] not in {"AUTO", "OFF"}:
                raise ValueError
            if customer["credit_limit"] is not None:
                limit = _native_decimal(customer["credit_limit"])
                if customer["credit_hold"] == "AUTO" and limit != "0":
                    raise ValueError
            if (
                currency["symbol"] != payload["currency"]["symbol"]
                or type(currency["precision"]) is not int
                or currency["precision"] != payload["currency"]["precision"]
            ):
                raise ValueError
            if not isinstance(meta["items"], list) or not isinstance(meta["locations"], list):
                raise ValueError
            items, item_ids = {}, set()
            for item in meta["items"]:
                _preview_keys(item, {"id", "sku", "type", "unitstype", "units"})
                for key in ("id", "unitstype", "units"):
                    _native_id(item[key])
                if (
                    item["sku"] in items
                    or item["id"] in item_ids
                    or item["type"] not in {"InvtPart", "Assembly", "Service"}
                ):
                    raise ValueError
                items[item["sku"]] = item
                item_ids.add(item["id"])
            if set(items) != {line["netsuite_sku"] for line in payload["lines"]}:
                raise ValueError
            locations = {}
            for location in meta["locations"]:
                _preview_keys(location, {"id", "subsidiary"})
                _native_id(location["id"])
                _native_id(location["subsidiary"])
                if location["id"] in locations:
                    raise ValueError
                locations[location["id"]] = location["subsidiary"]
            if set(locations) != {line["location_id"] for line in payload["lines"]} or any(
                locations[line["location_id"]] != line["inventory_subsidiary_id"] for line in payload["lines"]
            ):
                raise ValueError
            record = preview["record"]
            _preview_keys(record, {"body", "lines", "billing_address", "shipping_address"})
            body = record["body"]
            expected_body = {
                **payload["expected_totals"],
                "entity": customer["id"],
                "subsidiary": payload["subsidiary_id"],
                "currency": currency["id"],
                "shipmethod": payload["shipping_method_id"],
                "customform": _native_id(body["customform"]),
                "terms": None if body["terms"] is None else _native_id(body["terms"]),
                "discountitem": None,
                "exchangerate": _native_decimal(body["exchangerate"], positive=True),
                "custbody_fw_solidus_order_total": payload["expected_totals"]["total"],
                "custbody_fw_solidus_tax_amount": payload["expected_totals"]["taxtotal"],
                "tranid": payload["order_reference"],
                "externalid": payload["external_id"],
                "trandate": payload["transaction_date"],
                "orderstatus": "A",
                "iscrosssubtransaction": payload["inventory_mode"] == "cross_subsidiary",
                "tobeemailed": False,
                "tobefaxed": False,
                "getauth": False,
                "paypalprocess": False,
            }
            for key, native in (("custom_form_id", "customform"), ("terms_id", "terms")):
                if payload[key] is not None and expected_body[native] != payload[key]:
                    raise ValueError
            if payload["tax_profile"]["mode"] == "aggregate_header":
                from decimal import ROUND_HALF_UP

                basis, tax = (_number(payload["expected_totals"][key]) for key in ("subtotal", "taxtotal"))
                rate = (
                    (tax * 100 / basis).quantize(Decimal("0.0000001"), rounding=ROUND_HALF_UP) if basis else Decimal(0)
                )
                if rate > 1000 or (tax and not rate):
                    raise ValueError
                expected_body.update(taxitem=payload["tax_profile"]["tax_code_id"], taxrate=_text(rate), istaxable=True)
            lines = record["lines"]
            if not isinstance(lines, list) or len(lines) != len(payload["lines"]):
                raise ValueError
            expected_lines = []
            for native, source in zip(lines, payload["lines"], strict=True):
                item = items[source["netsuite_sku"]]
                line = {
                    "item": item["id"],
                    "units": item["units"],
                    "quantity": source["quantity"],
                    "rate": source["rate"],
                    "amount": source["amount"],
                    "custcol_fw_vat_amount": source["tax_amount"],
                    "quantityfulfilled": "0",
                    "quantitybilled": "0",
                    "isclosed": False,
                    "createwo": False,
                    "createpo": "",
                    "price": "-1",
                    "inventory_unit_ids": sorted(source["inventory_unit_ids"]),
                    "original_sku": source["source_sku"],
                }
                if payload["inventory_mode"] == "cross_subsidiary":
                    line.update(
                        inventorylocation=source["location_id"], inventorysubsidiary=source["inventory_subsidiary_id"]
                    )
                else:
                    line["location"] = source["location_id"]
                if payload["tax_profile"]["mode"] == "line_tax_amount":
                    line.update(
                        taxcode=source["tax_code_id"],
                        tax1amt=source["tax_amount"],
                        taxrate1=_native_decimal(native["taxrate1"]),
                    )
                else:
                    line["istaxable"] = True
                expected_lines.append(line)
            expected = {
                "body": expected_body,
                "lines": expected_lines,
                **{key: payload[key] for key in ("billing_address", "shipping_address")},
            }
            # Serialization distinguishes true from 1 and false from 0 as well
            # as enforcing exact field membership and canonical decimal strings.
            if json.dumps(record, sort_keys=True) != json.dumps(expected, sort_keys=True):
                raise ValueError
            return preview
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, DecimalException):
        raise CreateInputError("create_preview_unproven") from None


def _preview_keys(value, keys):
    if not isinstance(value, dict) or set(value) != keys:
        raise CreateInputError("create_preview_unproven")


def _native_id(value):
    if not isinstance(value, str) or _id(value) != value:
        raise CreateInputError("create_preview_unproven")
    return value


def _native_decimal(value, *, positive=False):
    if not isinstance(value, str) or _text(value) != value or _number(value) < 0 or (positive and not _number(value)):
        raise CreateInputError("create_preview_unproven")
    return value
