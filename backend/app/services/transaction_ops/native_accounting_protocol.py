"""Exact native receipts and profile bindings, independent of MCP vendors.

These checks provide evidence, never grant approval or send a financial write.
"""

import json
from decimal import Decimal

from app.services.transaction_ops.accounting_preview import PreviewContractError, validate_receipt
from app.services.transaction_ops.native_accounting_profile import NativeAccountingProfile
from app.services.transaction_ops.netsuite_reader import _invalid_constant, _object
from app.services.transaction_ops.treatments import AMENDMENT_RECORD_TYPES as KINDS  # amendment kind -> document

WORK_FIELD = "custbody_ecom_tx_ops_work_key"
PROFILE_KEYS = ("schema_version", "account_id", "subsidiary_id", "role_id", "tax_regime", "fields")


def profile_binding(profile):
    parsed = NativeAccountingProfile.model_validate({k: v for k, v in profile.items() if k != "revision"})
    if parsed.enabled is not True:
        raise ValueError("native_profile_disabled")
    value = parsed.model_dump(mode="json")
    return {k: value[k] for k in PROFILE_KEYS}


def validate_capabilities(profile, receipt):
    if (
        receipt.get("success") is not True
        or type(receipt.get("schema_version")) is not int
        or receipt["schema_version"] != 1
        or type(receipt.get("financial_writes")) is not int
        or receipt["financial_writes"] != 0
        or receipt.get("execution_authorized") is not False
        or receipt.get("profile") != profile_binding(profile)
        or receipt.get("suitetax") is not False
        or set(receipt.get("treatments") or []) != set(KINDS)
    ):
        raise ValueError("native_capability_contract_mismatch")
    return {"available": True, "apply_enabled": receipt.get("apply_enabled") is True}


def validate_intent_profile(intent, profile):
    binding = profile_binding(profile)
    scope = intent["scope"]
    if (
        KINDS.get(intent.get("kind")) != intent.get("record_type")
        or scope["netsuite_account_id"] != binding["account_id"]
        or str(scope["subsidiary_id"]) != binding["subsidiary_id"]
        or str(intent["accounting_book"]) != profile["accounting_book_id"]
        or str(intent["ar_account"]) not in profile["ar_account_ids"]
        or str(intent["tax_account"]) not in profile["tax_account_ids"]
        or str(intent["sales_adjustment_account"]) not in profile["adjustment_account_ids"]
    ):
        raise ValueError("native_treatment_policy_mismatch")


def _same_field(field, actual, expected):
    if field == "istaxable":
        return type(actual) is bool and actual is expected
    if field == "taxitem":
        return actual == expected
    if actual is None or isinstance(actual, bool) or isinstance(expected, bool):
        return False
    try:
        left, right = Decimal(str(actual)), Decimal(str(expected))
        return left.is_finite() and right.is_finite() and left == right
    except ArithmeticError:
        return False


def verify_snapshot(before, after, amendment, expected, *, work_key=None):
    """Exact declared changes plus every observed protected field, including nulls.

    This independently repeats native checks. A save response and attribution
    alone cannot certify the source, posted ledger, or related applications.
    """
    body, lines = before["body"], before["lines"]
    current, current_lines = after["body"], after["lines"]
    if set(body) != set(current) or len(lines) != len(current_lines):
        raise ValueError("native_snapshot_shape_changed")
    changed = {"subtotal", "taxtotal", "total", *amendment.get("body", {})}
    if work_key is not None:
        changed.update({"lastmodifieddate", WORK_FIELD})
        if current.get(WORK_FIELD) != work_key:
            raise ValueError("native_operation_attribution_missing")
    if any(current[key] != value for key, value in body.items() if key not in changed):
        raise ValueError("native_protected_body_changed")
    for key, value in {**amendment.get("body", {}), **expected}.items():
        if not _same_field(key, current.get(key), value):
            raise ValueError("native_declared_body_mismatch")
    changes = {str(change["lineUniqueKey"]): change for change in amendment.get("lines", [])}
    if len(changes) != len(amendment.get("lines", [])):
        raise ValueError("native_duplicate_line_identity")
    seen = set()
    for old, new in zip(lines, current_lines, strict=True):
        key = old.get("lineuniquekey")
        if key in seen or set(old) != set(new) or (old.get("line"), key) != (new.get("line"), new.get("lineuniquekey")):
            raise ValueError("native_line_identity_changed")
        seen.add(key)
        change = changes.get(key) or {}
        fields = change.get("fields") or {}
        if change and str(change["line"]) != old.get("line"):
            raise ValueError("native_line_identity_changed")
        if any(new[k] != v for k, v in old.items() if k not in fields):
            raise ValueError("native_protected_line_changed")
        if any(not _same_field(k, new.get(k), v) for k, v in fields.items()):
            raise ValueError("native_declared_line_mismatch")
    if set(changes) - seen:
        raise ValueError("native_line_identity_missing")

    # Item/tax amendments must preserve the shipping/handling/discount residual.
    def residual(value):
        amounts = [Decimal(str(value[k])) for k in ("total", "subtotal", "taxtotal")]
        if any(not n.is_finite() or n != n.quantize(Decimal(".01")) for n in amounts):
            raise ValueError("native_monetary_precision_unsupported")
        return amounts[0] - amounts[1] - amounts[2]

    if residual(body) != residual(current):
        raise ValueError("native_amount_identity_changed")
    return True


def validate_preview(profile, request, response):
    if response.get("profile") != profile_binding(profile):
        raise ValueError("native_preview_profile_changed")
    if response.get("roleId") != profile["role_id"] or response.get("taxRegime") != profile["tax_regime"]:
        raise ValueError("native_preview_role_changed")
    if type(response.get("schema_version")) is not int or response["schema_version"] != 1:
        raise PreviewContractError("native_preview_schema_mismatch")
    # Reuse the existing public preview contract, with its explicit no-write proof.
    receipt = validate_receipt(request, {"success": response.get("success"), "result": json.dumps(response)})
    amendment = json.loads(request["amendmentJson"], object_pairs_hook=_object, parse_constant=_invalid_constant)
    expected = json.loads(request["expectedJson"], object_pairs_hook=_object, parse_constant=_invalid_constant)
    verify_snapshot(receipt["beforeSnapshot"], receipt["afterSnapshot"], amendment, expected)
    return receipt
