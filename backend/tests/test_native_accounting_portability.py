import json
from copy import deepcopy

from app.services.transaction_ops.accounting_field_map import LEGACY_FIELDS
from app.services.transaction_ops.accounting_preview import build_request
from app.services.transaction_ops.credit_reallocation import build_intent
from app.services.transaction_ops.source_line_alignment import build_intent as align
from tests.test_credit_reallocation import fixture
from tests.test_native_accounting_profile import profile


def renamed(value, fields):
    mapping = {LEGACY_FIELDS[key]: field for key, field in fields.items()}
    mapping["custcol_fw_item_sku"] = fields["original_sku"]
    if isinstance(value, list):
        return [renamed(v, fields) for v in value]
    if isinstance(value, dict):
        return {mapping.get(k, k): renamed(v, fields) for k, v in value.items()}
    return value


def test_second_customer_has_same_accounting_math_with_its_own_fields_and_no_fallback():
    source, review, evidence, support = fixture()
    evidence["sections"]["sales_order"]["line_evidence"]["lines"][0]["lineUniqueKey"] = "120"
    original = deepcopy((source, evidence, support))
    legacy = build_intent("tenant", "case", source, review, evidence, support)
    fields = profile()["fields"]
    evidence, support = renamed(evidence, fields), renamed(support, fields)
    intent = build_intent("tenant", "case", source, review, evidence, support, field_map=fields)
    assert intent and intent["expected_after"] == legacy["expected_after"]
    assert intent["expected_ledger"] == legacy["expected_ledger"]
    order = align(source, evidence, intent, field_map=fields)
    assert order and fields["vat_amount"] in order["proposed_fields"]["item"]["items"][0]
    request = build_request(order, account_id="test-account", subsidiary_id="1", currency_id="1", field_map=fields)
    assert fields["vat_amount"] in json.loads(request["amendmentJson"])["lines"][0]["fields"]
    assert json.loads(request["fieldMapJson"]) == fields
    assert "custcol_fw" not in request["amendmentJson"]
    assert build_intent("tenant", "case", source, review, original[1], original[2], field_map=fields) is None
    assert build_intent("tenant", "case", source, review, evidence, support) is None
    assert source == original[0]
