"""Read-only NetSuite evidence: exact identity, scope, precision and completeness."""

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.services.transaction_ops import netsuite_reader as reader

TENANT, CONNECTION = uuid.uuid4(), uuid.uuid4()
ACCOUNT, SUBSIDIARY, REFERENCE = "6738075", "5", "R123456789"


@pytest.fixture
def context(monkeypatch):
    connection = SimpleNamespace(
        id=CONNECTION, tenant_id=TENANT, provider="netsuite", status="active", encrypted_credentials="sealed"
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=Mock(scalar_one_or_none=Mock(return_value=connection))))
    monkeypatch.setattr(reader, "decrypt_credentials", Mock(return_value={"account_id": ACCOUNT}))
    monkeypatch.setattr(reader, "get_valid_token", AsyncMock(return_value="SECRET-TOKEN"))
    monkeypatch.setattr(reader, "set_tenant_context", AsyncMock())
    return db, connection


def lookup(items=None, **overrides):
    items = [{"id": "100", "type": "SalesOrd", "order_reference": REFERENCE}] if items is None else items
    return {"items": items, "count": len(items), "totalResults": len(items), "hasMore": False, **overrides}


def record(**overrides):
    return {
        "id": "100",
        "tranId": REFERENCE,
        "currency": {"id": "4", "refName": "EUR"},
        "subsidiary": {"id": SUBSIDIARY, "refName": "Framework EU"},
        "exchangeRate": 1.1,
        "subtotal": 2009,
        "taxTotal": 348.68,
        "total": 2009,
        "lastModifiedDate": "2026-09-04T00:00:00Z",
        "tranDate": "2026-09-01",
        "item": {
            "items": [
                {
                    "line": 1,
                    "amount": 10,
                    "quantity": 1,
                    "rate": 10,
                    "item": {"id": "42", "refName": "SKU-42"},
                    "description": "PII must not survive",
                }
            ],
            "totalResults": 1,
        },
        "taxDetails": {"items": [{"taxAmount": 1, "taxRate": 21, "taxBasis": 10}], "totalResults": 1},
        "email": "SECRET-CUSTOMER-EMAIL",
        "shippingAddress": {"address": "SECRET-ADDRESS"},
        **overrides,
    }


async def read(context, responses, **kwargs):
    requests = []
    currency_response = kwargs.pop("currency_response", {"id": "4", "symbol": "EUR", "currencyPrecision": 2})
    period_response = kwargs.pop("period_response", lookup([{"id": "10", "closed": "F", "alllocked": "F"}]))

    def respond(request):
        requests.append(request)
        if "/currency/" in request.url.path:
            response = currency_response
        elif request.method == "POST" and "FROM accountingperiod" in json.loads(request.content).get("q", ""):
            response = period_response
        else:
            response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await reader.read_netsuite_order(
            context[0],
            TENANT,
            CONNECTION,
            kwargs.pop("account_id", ACCOUNT),
            SUBSIDIARY,
            kwargs.pop("order_reference", REFERENCE),
            kwargs.pop("reference_field", "tranid"),
            client=client,
            **kwargs,
        )
    return result, requests


async def test_exact_lookup_projects_financial_fields_without_pii(context):
    result, requests = await read(context, [lookup(), record()])
    assert result["complete"] is True
    assert result["lookup"]["complete"] is True
    assert result["api_calls"] == 4
    assert requests[0].method == "POST" and requests[1].method == "GET"
    assert requests[0].url.params["limit"] == "2"
    query = json.loads(requests[0].content)["q"]
    assert "t.tranid = 'R123456789'" in query
    assert "t.type = 'SalesOrd'" in query
    assert "FETCH FIRST" not in query and "trandate >" not in query and "status =" not in query
    assert requests[1].url.params["expandSubResources"] == "true"
    assert result["orders"][0]["header"]["currency"] == {"id": "4", "refName": "EUR"}
    assert result["orders"][0]["header"]["exchangeRate"] == Decimal("1.1")
    assert result["orders"][0]["header"]["taxTotal"] == Decimal("348.68")
    assert result["orders"][0]["version"] == "2026-09-04T00:00:00Z"
    assert "SECRET" not in str(result) and "PII" not in str(result)
    assert "description" not in result["orders"][0]["lines"][0]
    assert reader.set_tenant_context.await_count == 2


async def test_uses_explicit_tenant_and_active_connection_predicates(context):
    await read(context, [lookup([])])
    statement = str(context[0].execute.call_args.args[0])
    assert "connections.tenant_id" in statement and "connections.id" in statement
    assert "connections.status" in statement and "connections.provider" in statement


@pytest.mark.parametrize(
    "changes", [{"tenant_id": uuid.uuid4()}, {"id": uuid.uuid4()}, {"status": "revoked"}, {"provider": "stripe"}]
)
async def test_wrong_connection_is_refused_even_if_db_returns_it(context, changes):
    for key, value in changes.items():
        setattr(context[1], key, value)
    with pytest.raises(reader.NetSuiteEvidenceError, match="connection"):
        await read(context, [])
    reader.get_valid_token.assert_not_awaited()


@pytest.mark.parametrize(
    "bad_field", ["t.id", "custbody_id;DELETE", "id FROM customer", "", "custbody_foo--", "custbody_foo\n"]
)
async def test_reference_field_must_be_safe_identifier(context, bad_field):
    with pytest.raises(reader.NetSuiteEvidenceError, match="reference_field"):
        await read(context, [], reference_field=bad_field)


@pytest.mark.parametrize("bad_account", ["different", "6738075.attacker.invalid", "6738075/evil", "6738075\n"])
async def test_target_account_is_verified_before_oauth(context, bad_account):
    with pytest.raises(reader.NetSuiteEvidenceError, match="account"):
        await read(context, [], account_id=bad_account)
    reader.get_valid_token.assert_not_awaited()


async def test_quotes_in_business_reference_cannot_change_sql(context):
    _, requests = await read(context, [lookup([])], order_reference="R'123")
    assert "t.tranid = 'R''123'" in json.loads(requests[0].content)["q"]


async def test_missing_record_only_complete_on_authoritative_empty_listing(context):
    result, _ = await read(context, [lookup([])])
    assert result["complete"] is True and result["orders"] == []


@pytest.mark.parametrize(
    "envelope",
    [
        {"items": []},
        lookup([], hasMore=True),
        lookup([], totalResults=1),
        lookup([], hasMore="false"),
        lookup([], totalResults="0"),
        lookup([], count=1),
    ],
)
async def test_partial_or_unknown_identity_listing_cannot_prove_missing(context, envelope):
    result, _ = await read(context, [envelope])
    assert result["complete"] is False and result["lookup"]["complete"] is False


async def test_two_records_are_bounded_and_duplicate_evidence_is_preserved(context):
    items = [{"id": str(i), "type": "SalesOrd", "order_reference": REFERENCE} for i in (100, 101)]
    result, requests = await read(context, [lookup(items, totalResults=7, hasMore=True), record(), record(id="101")])
    assert len(result["orders"]) == 2 and len(requests) == 5 and result["complete"] is False
    assert result["lookup"]["total_results"] == 7


async def test_decimal_json_never_rounds_amounts_through_float(context):
    raw = json.dumps(record()).replace('"taxTotal": 348.68', '"taxTotal": 123456789.123456789')
    result, _ = await read(context, [lookup(), httpx.Response(200, content=raw)])
    assert result["orders"][0]["header"]["taxTotal"] == Decimal("123456789.123456789")


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"subsidiary": {"id": "9"}}, "subsidiary_mismatch"),
        ({"subsidiary": None}, "missing_subsidiary"),
        ({"currency": None}, "missing_currency"),
        ({"id": "OTHER"}, "record_identity_mismatch"),
        ({"item": {"links": []}}, "lines_not_expanded"),
        ({"item": {"items": [], "totalResults": 3}}, "lines_incomplete"),
        ({"item": {"items": [], "hasMore": True}}, "lines_incomplete"),
        ({"item": {"items": [], "links": [{"rel": "next", "href": "https://attacker.invalid"}]}}, "lines_incomplete"),
        ({"taxDetails": {"links": []}}, "tax_details_not_expanded"),
        ({"lastModifiedDate": None}, "missing_version"),
    ],
)
async def test_record_completeness_is_explicit_and_never_follows_links(context, changes, reason):
    result, requests = await read(context, [lookup(), record(**changes)])
    assert result["complete"] is False
    assert reason in result["orders"][0]["completeness_errors"]
    assert all(request.url.host == "6738075.suitetalk.api.netsuite.com" for request in requests)


async def test_absent_tax_values_remain_absent_not_zero(context):
    body = record()
    body.pop("taxTotal")
    body.pop("taxDetails")
    result, _ = await read(context, [lookup(), body])
    assert "taxTotal" not in result["orders"][0]["header"]
    assert result["orders"][0]["tax_details"] is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, text="SECRET-CREDENTIALS"),
        httpx.Response(500, text="SECRET-PAYLOAD"),
        httpx.Response(200, text="SECRET-NOT-JSON"),
        httpx.Response(200, text='{"items":NaN}'),
        httpx.Response(200, text='{"items":[1e999999999999999999999]}'),
        httpx.Response(200, text='{"items":[],"items":["duplicate"]}'),
        httpx.Response(200, json={"items": "SECRET-WRONG-SHAPE"}),
        httpx.Response(
            200, json={"items": [{"id": "../customer/1", "type": "SalesOrd", "order_reference": REFERENCE}]}
        ),
        httpx.Response(200, json=lookup([{"id": "100", "type": "CustInvc", "order_reference": REFERENCE}])),
        httpx.Response(200, json=lookup([{"id": "100", "type": "SalesOrd", "order_reference": "wrong"}])),
    ],
)
async def test_invalid_response_errors_do_not_leak_upstream_text(context, response):
    with pytest.raises(reader.NetSuiteEvidenceError) as caught:
        await read(context, [response])
    assert "SECRET" not in str(caught.value)


async def test_response_byte_budget_is_enforced(context, monkeypatch):
    monkeypatch.setattr(reader, "MAX_RESPONSE_BYTES", 64)
    with pytest.raises(reader.NetSuiteEvidenceError, match="response_budget"):
        await read(context, [httpx.Response(200, content=" " * 65)])


async def test_no_oauth_token_fails_before_requests(context, monkeypatch):
    monkeypatch.setattr(reader, "get_valid_token", AsyncMock(return_value=None))
    with pytest.raises(reader.NetSuiteEvidenceError, match="authentication"):
        await read(context, [])
    assert reader.set_tenant_context.await_count == 2


async def test_currency_and_closed_period_are_explicit_metadata(context):
    result, _ = await read(
        context,
        [lookup(), record()],
        currency_response={"id": "4", "symbol": "EUR", "currencyPrecision": 2, "exchangeRate": 9.99},
        period_response=lookup([{"id": "10", "closed": "T", "alllocked": "T", "arlocked": "T"}]),
    )
    order = result["orders"][0]
    assert order["currency_metadata"] == {"id": "4", "symbol": "EUR", "currencyPrecision": 2}
    assert order["header"]["exchangeRate"] == Decimal("1.1")
    assert order["periods"]["items"][0]["closed"] == "T"
    assert order["complete"] is True  # complete evidence does not mean eligible to mutate


async def test_partial_period_lookup_marks_evidence_incomplete(context):
    result, _ = await read(context, [lookup(), record()], period_response=lookup([], hasMore=True))
    assert "period_lookup_incomplete" in result["orders"][0]["completeness_errors"]


async def test_account_currency_requires_matching_record_identity(context):
    with pytest.raises(reader.NetSuiteEvidenceError, match="currency_identity_mismatch"):
        await read(context, [lookup(), record()], currency_response={"id": "5", "symbol": "USD"})


async def test_live_framework_custom_fields_survive_allowlisted_projection(context):
    body = record(custbody_fw_solidus_order_total=2009, taxRate=21)
    body["item"]["items"][0].update(
        {
            "custcol_fw_solidus_line_id": "12345",
            "custcol_fw_vat_amount": 232.39,
            "custcol_fw_item_sku": "SKU-42",
            "custcol_fw_item_rate": 1339,
        }
    )
    result, _ = await read(context, [lookup(), body])
    line = result["orders"][0]["lines"][0]
    assert line["custcol_fw_vat_amount"] == Decimal("232.39")
    assert line["custcol_fw_solidus_line_id"] == "12345"
    assert result["orders"][0]["header"]["custbody_fw_solidus_order_total"] == 2009


async def test_customer_identity_is_retained_without_customer_name_or_email(context):
    result, _ = await read(
        context, [lookup(), record(entity={"id": "40", "refName": "Private name", "email": "private@example.test"})]
    )
    assert result["orders"][0]["header"]["entity"] == {"id": "40"}


async def test_reference_change_between_lookup_and_record_read_is_not_authoritative_match(context):
    result, _ = await read(context, [lookup(), record(tranId="DIFFERENT")])
    assert result["complete"] is False
    assert "record_reference_mismatch" in result["orders"][0]["completeness_errors"]


async def test_configured_custom_reference_is_verified_and_projected(context):
    result, _ = await read(
        context, [lookup(), record(custbody_framework_order=REFERENCE)], reference_field="custbody_framework_order"
    )
    assert result["complete"] is True
    assert result["orders"][0]["header"]["custbody_framework_order"] == REFERENCE


async def test_configured_reference_missing_from_record_is_not_proven(context):
    result, _ = await read(context, [lookup(), record()], reference_field="custbody_framework_order")
    assert "record_reference_mismatch" in result["orders"][0]["completeness_errors"]


async def test_request_budget_is_enforced_before_additional_external_read(context, monkeypatch):
    monkeypatch.setattr(reader, "MAX_API_CALLS", 1)
    with pytest.raises(reader.NetSuiteEvidenceError, match="api_call_budget"):
        await read(context, [lookup()])
