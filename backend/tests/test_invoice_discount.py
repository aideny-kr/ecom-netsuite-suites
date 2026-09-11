from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.chat.write_payload import normalize_write_payload
from app.services.transaction_ops.invoice_discount import review_for_card, verify_after
from app.services.transaction_ops.sales_credit import build_candidate
from tests.test_sales_credit import inputs


def unpaid_inputs():
    d = inputs(paid="0")
    s = d["support"]
    d["source"].update(tax_total="0", additional_tax_total="0", total="95", adjustment_total="-5")
    s["invoice"].update(
        total="100",
        taxTotal="0",
        taxRate="0",
        amountRemaining="100",
        status={"id": "Open"},
        postingPeriod={"id": "171"},
    )
    s["invoice_gl"] = {
        "complete": True,
        "rows": [
            {"account": "100", "accountingbook": "1", "debit": "100"},
            {"account": "200", "accountingbook": "1", "credit": "100"},
        ],
    }
    s["invoice_lines"] = [
        {
            "line": "1",
            "item": {"id": "70"},
            "itemType": {"id": "InvtPart"},
            "amount": "100",
            "quantity": "1",
            "rate": "100",
        }
    ]
    s["invoice_lines_complete"] = True
    return d


def test_unpaid_invoice_gets_discount_instead_of_credit():
    p = build_candidate(**unpaid_inputs())
    assert p["kind"] == "invoice_sales_adjustment"
    assert (p["record_type"], p["mutation_type"]) == ("invoice", "update")
    assert p["proposed_fields"] == {"discountItem": {"id": "50"}, "discountRate": -5.0}
    assert p["expected_after"] == {
        "total": "95",
        "taxTotal": "0.00",
        "amountPaid": "0.00",
        "amountRemaining": "95",
        "discountTotal": "-5",
    }


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("invoice", "amountPaid", "10"),
        ("invoice", "amountPaid", None),
        ("invoice", "discountTotal", "-1"),
        ("invoice", "discountItem", {"id": "50"}),
        ("invoice", "postingPeriod", {"id": "170"}),
        ("invoice", "taxRate", None),
        ("period", "arLocked", True),
        ("period", "closed", True),
        ("item", "nonPosting", True),
        ("item", "isInactive", True),
        ("item", "subsidiary", {"items": [{"id": "2"}], "count": 1, "totalResults": 1, "hasMore": False}),
        ("item", "subsidiary", {"items": [{"id": "1"}], "count": 1, "totalResults": 2, "hasMore": True}),
        ("applications", "links", [{"type": "CustCred"}]),
    ],
)
def test_unsafe_invoice_never_falls_back_to_credit(section, key, value):
    d = unpaid_inputs()
    d["support"][section][key] = value
    assert build_candidate(**d) is None


@pytest.mark.parametrize("variant", ["missing", "discount", "amount"])
def test_invoice_line_evidence_required(variant):
    d = unpaid_inputs()
    s = d["support"]
    if variant == "missing":
        s["invoice_lines_complete"] = False
    elif variant == "discount":
        s["invoice_lines"][0]["itemType"] = {"id": "Discount"}
    else:
        s["invoice_lines"][0]["amount"] = "99"
    assert build_candidate(**d) is None


def test_discount_guard_requires_exact_scope_and_amount(monkeypatch):
    p = build_candidate(**unpaid_inputs())
    p["observed_at"] = datetime.now(timezone.utc).isoformat()
    db = SimpleNamespace(info={"accounting_correction_candidate": p})
    monkeypatch.setattr("app.services.chat.tools.parse_external_tool_name", lambda _: ("connector", "ns_updateRecord"))
    n = normalize_write_payload({"recordId": "20", "data": p["proposed_fields"]})
    assert review_for_card(db, "tenant", "tool", "invoice", n) == p
    for tenant, record, fields in [
        ("other", "20", p["proposed_fields"]),
        ("tenant", "21", p["proposed_fields"]),
        ("tenant", "20", {"discountRate": -50}),
    ]:
        with pytest.raises(ValueError):
            review_for_card(
                db, tenant, "tool", "invoice", normalize_write_payload({"recordId": record, "data": fields})
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant", ["valid", "payment", "discount", "item", "location", "tax", "gl", "partial", "period", "application"]
)
async def test_native_verification(variant):
    p = build_candidate(**unpaid_inputs())
    doc = {
        **deepcopy(p["before"]),
        **p["expected_after"],
        **p["proposed_fields"],
        "item": {"items": deepcopy(p["support"]["invoice_lines"])},
    }
    doc["discountRate"] = str(doc["discountRate"])
    rows = [
        {"account": "100", "accountingbook": "1", "debit": "95"},
        {"account": "200", "accountingbook": "1", "credit": "100"},
        {"account": "500", "accountingbook": "1", "debit": "5"},
    ]
    if variant == "payment":
        doc["amountPaid"] = "1"
    elif variant == "discount":
        doc["discountItem"] = {"id": "99"}
    elif variant == "item":
        doc["item"]["items"][0]["item"]["id"] = "99"
    elif variant == "location":
        doc["location"] = {"id": "99"}
    elif variant == "tax":
        doc["taxTotal"] = "0.01"
    elif variant == "gl":
        rows[2]["account"] = "999"
    elif variant == "period":
        doc["postingPeriod"] = {"id": "170"}

    @asynccontextmanager
    async def reader(*args, **kwargs):
        async def request(method, path, **kw):
            assert method == "GET" or path == "/query/v1/suiteql"
            if "nexttransactionlinelink" in kw.get("body", {}).get("q", ""):
                values = [{"nextdoc": "99"}] if variant == "application" else []
                return {"items": values, "count": len(values), "totalResults": len(values), "hasMore": False}
            return (
                doc
                if method == "GET"
                else {"items": rows, "hasMore": variant == "partial", "count": 3, "totalResults": 3}
            )

        yield SimpleNamespace(request=request)

    with patch("app.services.transaction_ops.netsuite_reader.authenticated_reader", reader):
        result = await verify_after(None, "tenant", p)
    assert result["status"] == ("verified" if variant == "valid" else "needs_review")
    assert result["retry_allowed"] is False
    assert result["cash_settlement"] == "not_verified"


@pytest.mark.parametrize("variant", ["valid", "wrong_account", "missing_item", "paid", "tax", "missing_links"])
def test_reconciliation_matches_only_proven_invoice_discount(variant):
    from app.services.transaction_ops.order_reconciliation import reconcile_order
    from tests.test_commercial_credits import recon_fixture

    source, target, config, refunds = recon_fixture()
    d = unpaid_inputs()
    s = d["support"]
    row = d["source"]
    row.update(number="R100", business_entity="US")
    source["orders"] = [row]
    order = target["orders"][0]
    order["header"].update(total="100", taxTotal="0")
    invoice = {
        **s["invoice"],
        "createdFrom": {"id": "5"},
        "total": "95",
        "amountRemaining": "95",
        "discountItem": {"id": "50"},
        "discountRate": "-5",
        "discountTotal": "-5",
    }
    gl = {
        "complete": True,
        "rows": [
            {"account": "100", "accountingbook": "1", "debit": "95"},
            {"account": "200", "accountingbook": "1", "credit": "100"},
            {"account": "500", "accountingbook": "1", "debit": "5"},
        ],
    }
    proof = target["commercial_credit_evidence"]
    proof["sections"] = {
        "posting_documents": [invoice],
        "invoice_applications": s["applications"],
        "gl": {"20": gl},
        "invoice_discount_item": s["item"],
    }
    if variant == "wrong_account":
        gl["rows"][2]["account"] = "999"
    elif variant == "missing_item":
        proof["sections"].pop("invoice_discount_item")
    elif variant == "paid":
        invoice["amountPaid"] = "1"
    elif variant == "tax":
        invoice["taxTotal"] = "0.01"
    elif variant == "missing_links":
        s["applications"]["complete"] = False
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == ("matched" if variant == "valid" else "difference")
    if variant == "valid":
        assert result["original_amounts"]["order_total"]["delta"] == "-5.00"
        assert result["adjustments"][0]["kind"] == "posted_invoice_discount"
        assert "credit_memo_id" not in result["adjustments"][0]


@pytest.mark.asyncio
async def test_conflicting_receipt_cannot_be_verified():
    result = await verify_after(None, "tenant", build_candidate(**unpaid_inputs()), {"recordId": "99"})
    assert result == {"status": "needs_review", "reason": "invoice_receipt_identity_conflict", "retry_allowed": False}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant", ["valid", "payment", "period", "item", "source", "config", "connector", "tenant", "payload"]
)
async def test_approval_rechecks_current_accounting_state(variant, monkeypatch):
    import json
    from unittest.mock import AsyncMock
    from uuid import UUID

    from app.services.transaction_ops.invoice_discount import validate_approved

    d = unpaid_inputs()
    d["case_id"] = "11111111-1111-4111-8111-111111111111"
    d["now"] = datetime.now(timezone.utc)
    d["support"]["observed_at"] = d["now"].isoformat()
    for refund in d["support"]["refunds"].values():
        refund["observed_at"] = d["now"].isoformat()
    p = build_candidate(**d)
    db = SimpleNamespace(
        scalar=AsyncMock(
            return_value=SimpleNamespace(
                id=UUID(d["case_id"]),
                status="open",
                order_reference=p["order_reference"],
                scope_json={},
                latest_report_json=d["report"],
            )
        )
    )
    params = {"recordType": "invoice", "recordId": "20", "data": json.dumps(p["proposed_fields"])}
    current = deepcopy(d)
    if variant == "payment":
        current["support"]["invoice"]["amountPaid"] = "1"
    elif variant == "period":
        current["support"]["period"]["closed"] = True
    elif variant == "item":
        current["support"]["item"]["nonPosting"] = True
    elif variant == "source":
        current["source"]["total"] = "94"
    elif variant == "config":
        current["review"]["config_id"] = "other"
    elif variant == "payload":
        params["data"] = json.dumps({"discountRate": -50})
    monkeypatch.setattr("app.services.chat.tools.parse_external_tool_name", lambda _: ("connector", "ns_updateRecord"))
    monkeypatch.setattr(
        "app.services.mcp_connector_service.get_mcp_connector",
        AsyncMock(
            return_value=SimpleNamespace(
                status="active",
                is_enabled=True,
                server_url="https://wrong.example"
                if variant == "connector"
                else "https://123.suitetalk.api.netsuite.com/services/mcp/v1/all",
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.transaction_ops.accounting_review.accounting_context", AsyncMock(return_value=current["review"])
    )
    monkeypatch.setattr(
        "app.services.transaction_ops.tax_correction.refresh_source", AsyncMock(return_value=current["source"])
    )
    monkeypatch.setattr(
        "app.services.transaction_ops.accounting_evidence.collect_accounting_evidence", AsyncMock(return_value={})
    )
    monkeypatch.setattr("app.services.transaction_ops.commercial_credits.collect_commercial_credits", AsyncMock())
    monkeypatch.setattr(
        "app.services.transaction_ops.sales_credit.collect_support", AsyncMock(return_value=current["support"])
    )
    if variant == "valid":
        await validate_approved(db, "tenant", "tool", params, p)
    else:
        with pytest.raises(ValueError):
            await validate_approved(db, "other" if variant == "tenant" else "tenant", "tool", params, p)
