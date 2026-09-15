from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.chat.write_payload import normalize_write_payload
from app.services.transaction_ops import credit_api_correction as api
from tests.test_native_accounting_service import prepared


def schema(fields):
    props = {k: {"type": "number"} for k in fields}
    props["item"] = {
        "properties": {
            "items": {
                "items": {
                    "properties": {k: {"type": "number"} for k in fields["item"]["items"][0]},
                    "x-ns-sublistkey": {"value": {"existing": ["line"]}},
                }
            }
        }
    }
    return {"properties": props}


def proposed():
    p, _ = prepared()
    p["execution_transport"] = "mcp_record_api"
    p["connector_id"] = str(uuid4())
    return p


def applied(p):
    support = deepcopy(p["support"])
    credit = support["credit"]
    credit.update(p["expected_after"])
    credit.update({k: v for k, v in p["proposed_fields"].items() if k != "item"})
    credit["line_evidence"]["lines"][0].update(p["proposed_fields"]["item"]["items"][0])
    support["credit_gl"]["rows"] = [
        {
            "account": account,
            "accountingbook": p["accounting_book"],
            "debit": amount if side == "debit" else "0",
            "credit": amount if side == "credit" else "0",
        }
        for side, entries in p["expected_ledger"].items()
        for account, amount in entries.items()
    ]
    return support


@pytest.mark.parametrize("change", ["missing_tax", "read_only_line", "unkeyed_lines"])
def test_unsupported_schema_cannot_prepare_credit(change):
    p = proposed()
    raw = schema(p["proposed_fields"])
    if change == "missing_tax":
        raw["properties"].pop("taxItem")
    elif change == "read_only_line":
        raw["properties"]["item"]["properties"]["items"]["items"]["properties"]["amount"]["readOnly"] = True
    else:
        raw["properties"]["item"]["properties"]["items"]["items"]["x-ns-sublistkey"] = {}
    with pytest.raises(ValueError, match="credit_api"):
        api.schema_contract(raw, p["proposed_fields"])


def test_keyed_payload_is_bound_to_connector_record_and_exact_lines():
    p = proposed()
    db = SimpleNamespace(info={"accounting_correction_candidate": p})
    tool = f"ext__{p['connector_id'].replace('-', '')}__ns_updateRecord"
    data = {"recordType": "creditmemo", "recordId": p["record_id"], "data": p["proposed_fields"]}
    assert api.review_for_card(db, p["tenant_id"], tool, "creditmemo", normalize_write_payload(data)) == p
    wrong = deepcopy(data)
    wrong["data"]["item"]["items"][0]["amount"] = "399"
    with pytest.raises(ValueError, match="binding_changed"):
        api.review_for_card(db, p["tenant_id"], tool, "creditmemo", normalize_write_payload(wrong))


def test_credit_candidate_does_not_intercept_an_unrelated_customer_operation():
    from app.services.transaction_ops.tax_correction import review_for_card

    p = proposed()
    db = SimpleNamespace(info={"accounting_correction_candidate": p})
    tool = f"ext__{p['connector_id'].replace('-', '')}__ns_updateRecord"
    record = normalize_write_payload({"recordId": "99", "data": {"companyName": "Updated name"}})
    assert review_for_card(db, p["tenant_id"], tool, "customer", record) is None


def test_existing_approval_age_does_not_skip_fresh_preflight():
    p = proposed()
    p["observed_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db = SimpleNamespace(info={"accounting_correction_candidate": p})
    tool = f"ext__{p['connector_id'].replace('-', '')}__ns_updateRecord"
    data = normalize_write_payload({"recordId": p["record_id"], "data": p["proposed_fields"]})
    with pytest.raises(ValueError):
        api.review_for_card(db, p["tenant_id"], tool, "creditmemo", data)
    assert api.review_for_card(db, p["tenant_id"], tool, "creditmemo", data, check_age=False) == p


def test_exact_ledger_and_related_records_must_verify():
    p = proposed()
    api.verify_evidence(p, applied(p))


async def test_preparation_preserves_exact_decimals_in_persistable_evidence(monkeypatch):
    import json
    from contextlib import asynccontextmanager
    from decimal import Decimal
    from unittest.mock import AsyncMock

    p, data = prepared()
    p["before"]["subtotal"] = Decimal("440.00")
    p["support"]["credit"]["subtotal"] = Decimal("440.00")
    p["support"]["credit"].pop("taxTotal", None)
    p["before"] = p["support"]["credit"]
    reader = SimpleNamespace(request=AsyncMock(return_value=schema(p["proposed_fields"])))

    @asynccontextmanager
    async def connection(*args, **kwargs):
        yield reader

    monkeypatch.setattr(api, "authenticated_reader", connection)
    result = await api.prepare(AsyncMock(), p["tenant_id"], p, data[2], None)
    assert json.loads(json.dumps(result))["support"]["credit"]["subtotal"] == "440.00"
    assert result["before"]["subtotal"] == "440.00"
    assert result["before"]["taxTotal"] == "0.00"
    assert "taxTotal" not in result["support"]["credit"]
    assert "taxTotal" not in p["support"]["credit"]


@pytest.mark.parametrize("change", ["tax", "gl", "invoice", "refund", "application", "line", "book"])
def test_wrong_amounts_or_changed_protected_records_never_report_success(change):
    p = proposed()
    support = applied(p)
    if change == "tax":
        support["credit"]["taxTotal"] = "39.99"
    elif change == "gl":
        support["credit_gl"]["rows"][0]["debit"] = "399"
    elif change in {"invoice", "refund"}:
        support[change]["total"] = "1"
    elif change == "application":
        support["credit"]["application_evidence"]["lines"] = []
    elif change == "line":
        support["credit"]["line_evidence"]["lines"][0]["lineUniqueKey"] = "wrong"
    else:
        support["credit_gl"]["rows"][0]["accountingbook"] = "2"
    with pytest.raises(ValueError):
        api.verify_evidence(p, support)


@pytest.mark.parametrize(
    "drift", [None, "connection", "source", "sales_order", "incomplete", "subledger", "treatment", "schema"]
)
async def test_actual_preflight_rejects_changed_evidence_before_dispatch(monkeypatch, drift):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from app.services.transaction_ops import accounting_evidence, accounting_review, case_service, tax_correction

    p, data = prepared()
    source, review, evidence, support = deepcopy(data[:4])
    p.update(execution_transport="mcp_record_api", case_id=str(uuid4()), connector_id=str(uuid4()))
    p["protected_sales_order"] = deepcopy(evidence["sections"]["sales_order"])
    p["connector_schema"] = api.schema_contract(schema(p["proposed_fields"]), p["proposed_fields"])
    review.update(
        scope=p["scope"],
        config_id=p["config_id"],
        netsuite_connection_id=p["connection_id"],
        native_mcp_connector_id=p["connector_id"],
        connection_active=True,
    )
    raw = schema(p["proposed_fields"])
    if drift == "connection":
        review["native_mcp_connector_id"] = str(uuid4())
    elif drift == "source":
        source["total"] = "1"
    elif drift == "sales_order":
        evidence["sections"]["sales_order"]["total"] = "1"
    elif drift == "incomplete":
        support = None
    elif drift == "subledger":
        support["invoice"]["total"] = "1"
    elif drift == "treatment":
        p["expected_after"]["taxTotal"] = "1"
    elif drift == "schema":
        raw["properties"]["taxItem"]["description"] = "changed contract"
    monkeypatch.setattr(api.case_resolution_scope, "validate", AsyncMock())
    monkeypatch.setattr(
        case_service, "get_case", AsyncMock(return_value=SimpleNamespace(scope_json={}, latest_report_json={}))
    )
    monkeypatch.setattr(accounting_review, "accounting_context", AsyncMock(return_value=review))
    monkeypatch.setattr(tax_correction, "refresh_source", AsyncMock(return_value=source))
    monkeypatch.setattr(accounting_evidence, "collect_accounting_evidence", AsyncMock(return_value=evidence))
    monkeypatch.setattr(api, "collect_support", AsyncMock(return_value=support))
    reader = SimpleNamespace(request=AsyncMock(return_value=raw))

    @asynccontextmanager
    async def connection(*args, **kwargs):
        yield reader

    monkeypatch.setattr(api, "authenticated_reader", connection)
    tool = f"ext__{p['connector_id'].replace('-', '')}__ns_updateRecord"
    payload = {"recordType": "creditmemo", "recordId": p["record_id"], "data": p["proposed_fields"]}
    db = SimpleNamespace(info={})
    if drift:
        reason = {
            "connection": "connection_scope_changed",
            "source": "source_changed",
            "sales_order": "protected_sales_order_changed",
            "incomplete": "subledger_incomplete",
            "subledger": "subledger_changed",
            "treatment": "treatment_changed",
            "schema": "schema_changed",
        }[drift]
        with pytest.raises(ValueError, match=reason):
            await api.validate_approved(db, p["tenant_id"], tool, payload, p)
    else:
        await api.validate_approved(db, p["tenant_id"], tool, payload, p)
        assert reader.request.await_args.args == ("GET", "/record/v1/metadata-catalog/creditMemo")
