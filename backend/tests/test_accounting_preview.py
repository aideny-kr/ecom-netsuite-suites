import json

import pytest

from app.services.transaction_ops.accounting_preview import PreviewContractError, build_request, validate_receipt
from app.services.transaction_ops.credit_reallocation import build_intent
from tests.test_credit_reallocation import fixture


def request_and_intent():
    source, review, evidence, support = fixture()
    intent = build_intent("tenant", "case", source, review, evidence, support)
    return build_request(intent, account_id="TEST_SB1", subsidiary_id="1", currency_id="1"), intent


def response(request):
    return {
        "success": True,
        "result": json.dumps(
            {
                **{k: request[k] for k in ("accountId", "recordType", "recordId", "subsidiaryId", "currencyId")},
                "amendment": json.loads(request["amendmentJson"]),
                "after": json.loads(request["expectedJson"]),
                "matches": True,
                "saved": False,
                "financialWrites": 0,
                "executionAuthorized": False,
            }
        ),
    }


def test_preview_preserves_native_keys_and_is_never_an_approval():
    request, _ = request_and_intent()
    amendment = json.loads(request["amendmentJson"])
    assert request["accountId"] == "test-sb1"
    assert amendment["body"] == {"taxitem": "61", "taxrate": "10.0000000", "istaxable": True}
    assert amendment["lines"][0] == {
        "line": "1",
        "lineUniqueKey": "100",
        "fields": {"rate": "400", "amount": "400", "istaxable": True},
    }
    receipt = validate_receipt(request, response(request))
    assert receipt["executable"] is False and receipt["financial_write_authorized"] is False


@pytest.mark.parametrize("tamper", ["account", "record", "line", "amount", "saved", "authorized", "bool_write_count"])
def test_native_receipt_must_match_the_exact_preview(tamper):
    request, _ = request_and_intent()
    data = response(request)
    receipt = json.loads(data["result"])
    if tamper == "account":
        receipt["accountId"] = "another"
    if tamper == "record":
        receipt["recordId"] = "999"
    if tamper == "line":
        receipt["amendment"]["lines"][0]["lineUniqueKey"] = "999"
    if tamper == "amount":
        receipt["after"]["total"] = "440.01"
    if tamper == "saved":
        receipt["saved"] = True
    if tamper == "authorized":
        receipt["executionAuthorized"] = True
    if tamper == "bool_write_count":
        receipt["financialWrites"] = False
    data["result"] = json.dumps(receipt)
    with pytest.raises(PreviewContractError):
        validate_receipt(request, data)


@pytest.mark.parametrize("tamper", ["quantity", "record_type", "duplicate_line", "source_id", "nonfinite"])
def test_preview_rejects_unapproved_fields_and_ambiguous_identity(tamper):
    _, intent = request_and_intent()
    line = intent["proposed_fields"]["item"]["items"][0]
    if tamper == "quantity":
        line["quantity"] = "2"
    if tamper == "record_type":
        intent["record_type"] = "invoice"
    if tamper == "duplicate_line":
        intent["proposed_fields"]["item"]["items"].append(line)
    if tamper == "source_id":
        line["line"] = "101"
    if tamper == "nonfinite":
        line["rate"] = "NaN"
    with pytest.raises(PreviewContractError):
        build_request(intent, account_id="TEST", subsidiary_id="1", currency_id="1")


def test_zero_tax_rate_uses_native_decimal_not_scientific_notation():
    _, intent = request_and_intent()
    intent["proposed_fields"]["taxRate"] = "0E-7"
    request = build_request(intent, account_id="TEST", subsidiary_id="1", currency_id="1")
    assert json.loads(request["amendmentJson"])["body"]["taxrate"] == "0.0000000"
