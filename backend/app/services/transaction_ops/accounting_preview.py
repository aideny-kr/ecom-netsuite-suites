"""Translate evidence-backed intents into a native, unsaved calculation request.

This module performs no I/O, creates no approvals and cannot authorize writes.
Native preview availability and save-time behavior are separate capabilities.
"""

import json
import re
from decimal import Decimal

from app.services.transaction_ops.accounting_field_map import LEGACY_FIELDS
from app.services.transaction_ops.credit_classification import reference

BODY = {"taxItem": "taxitem", "taxRate": "taxrate", "taxTotal": "taxtotal", "isTaxable": "istaxable"}
LINE = {"rate": "rate", "amount": "amount", "isTaxable": "istaxable", "custcol_fw_vat_amount": "custcol_fw_vat_amount"}
AMOUNTS = {"subtotal": "subtotal", "taxTotal": "taxtotal", "total": "total"}


class PreviewContractError(ValueError):
    pass


def _id(value):
    text = str(value)
    if not re.fullmatch(r"[1-9][0-9]*", text):
        raise PreviewContractError("native_identity_required")
    return text


def _decimal(value):
    text = str(value)
    if not re.fullmatch(r"-?\d{1,12}(\.\d{1,7})?([eE][+-]?\d{1,2})?", text):
        raise PreviewContractError("bounded_decimal_required")
    text = format(Decimal(text), "f")
    if not re.fullmatch(r"-?\d{1,12}(\.\d{1,7})?", text):
        raise PreviewContractError("bounded_decimal_required")
    return text


def _field(key, value):
    if key == "isTaxable":
        if type(value) is not bool:
            raise PreviewContractError("boolean_required")
        return value
    if key == "taxItem":
        if not isinstance(value, dict) or set(value) != {"id"}:
            raise PreviewContractError("tax_item_identity_required")
        return _id(value["id"])
    return _decimal(value)


def build_request(intent, *, account_id, subsidiary_id, currency_id, field_map=None):
    """Retain native line IDs; never use source IDs or array positions as targets."""
    try:
        from app.services.transaction_ops.native_accounting_profile import NativeFieldMap

        fields_map = NativeFieldMap.model_validate(LEGACY_FIELDS if field_map is None else field_map).model_dump()
        line_fields = {
            "rate": "rate",
            "amount": "amount",
            "isTaxable": "istaxable",
            fields_map["vat_amount"]: fields_map["vat_amount"],
        }
        expected_type = {"credit_tax_reallocation": "creditmemo", "sales_order_line_alignment": "salesorder"}
        if expected_type.get(intent["kind"]) != intent["record_type"]:
            raise PreviewContractError("unsupported_preview_kind")
        fields = intent["proposed_fields"]
        if set(fields) - (set(BODY) | {"item"}):
            raise PreviewContractError("unsupported_preview_field")
        body = {BODY[key]: _field(key, value) for key, value in fields.items() if key != "item"}
        item = fields.get("item", {"items": []})
        if set(item) != {"items"} or not isinstance(item["items"], list) or len(item["items"]) > 30:
            raise PreviewContractError("bounded_item_changes_required")
        if intent["record_type"] == "creditmemo":
            observed = intent["before"]["line_evidence"]
            if observed.get("complete") is not True:
                raise PreviewContractError("complete_native_lines_required")
            identities = [(line["line"], line["lineUniqueKey"]) for line in observed["lines"]]
        else:
            identities = [(line["target_line"], line["target_line_unique_key"]) for line in intent["line_identities"]]
        keys = {_id(line): _id(key) for line, key in identities}
        if len(keys) != len(identities) or len(set(keys.values())) != len(keys):
            raise PreviewContractError("ambiguous_native_line_identity")
        lines, seen = [], set()
        for proposed in item["items"]:
            if set(proposed) - (set(line_fields) | {"line"}) or not (set(proposed) & set(line_fields)):
                raise PreviewContractError("unsupported_preview_line_field")
            line = _id(proposed["line"])
            if line not in keys or line in seen:
                raise PreviewContractError("native_line_identity_required")
            seen.add(line)
            lines.append(
                {
                    "line": line,
                    "lineUniqueKey": keys[line],
                    "fields": {line_fields[k]: _field(k, v) for k, v in proposed.items() if k != "line"},
                }
            )
        if not body and not lines:
            raise PreviewContractError("empty_preview")
        expected = {native: _decimal(intent["expected_after"][key]) for key, native in AMOUNTS.items()}
        account = str(account_id).replace("_", "-").lower()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", account):
            raise PreviewContractError("account_identity_required")
        request = {
            "accountId": account,
            "recordType": intent["record_type"],
            "recordId": _id(intent["record_id"]),
            "subsidiaryId": _id(subsidiary_id),
            "currencyId": _id(currency_id),
            "fieldMapJson": json.dumps(fields_map, sort_keys=True, separators=(",", ":")),
            "amendmentJson": json.dumps({"body": body, "lines": lines}, sort_keys=True, separators=(",", ":")),
            "expectedJson": json.dumps(expected, sort_keys=True, separators=(",", ":")),
        }
        if len(request["amendmentJson"]) > 32000:
            raise PreviewContractError("preview_size_limit")
        return request
    except (KeyError, TypeError) as exc:
        raise PreviewContractError("incomplete_preview_intent") from exc


def for_intent(intent, review, native_record, *, field_map=None):
    return build_request(
        intent,
        account_id=review["scope"]["netsuite_account_id"],
        subsidiary_id=review["scope"]["subsidiary_id"],
        currency_id=reference(native_record, "currency"),
        field_map=field_map,
    )


def validate_receipt(request, response):
    """A successful native calculation is evidence only, never approval authority."""
    try:
        if response.get("success") is not True or not isinstance(response.get("result"), str):
            raise PreviewContractError("native_preview_failed")
        if len(response["result"]) > 80000:
            raise PreviewContractError("preview_receipt_limit")
        receipt = json.loads(response["result"])
        if any(
            receipt.get(k) != request[k] for k in ("accountId", "recordType", "recordId", "subsidiaryId", "currencyId")
        ):
            raise PreviewContractError("preview_scope_mismatch")
        if receipt.get("amendment") != json.loads(request["amendmentJson"]):
            raise PreviewContractError("preview_amendment_mismatch")
        if (
            receipt.get("saved") is not False
            or type(receipt.get("financialWrites")) is not int
            or receipt["financialWrites"] != 0
            or receipt.get("executionAuthorized") is not False
        ):
            raise PreviewContractError("invalid_preview_authority")
        expected = json.loads(request["expectedJson"])
        if receipt.get("matches") is not True or any(
            Decimal(_decimal(receipt["after"][key])) != Decimal(value) for key, value in expected.items()
        ):
            raise PreviewContractError("native_amount_mismatch")
        return {**receipt, "executable": False, "financial_write_authorized": False}
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise PreviewContractError("invalid_preview_receipt") from exc
