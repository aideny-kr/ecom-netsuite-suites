"""Exercise current account controls and native support reads before a credit."""

import json
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.transaction_ops.sales_credit import build_candidate, collect_support, validate_approved
from tests.test_accounting_approval_flow import inputs as tool_inputs
from tests.test_sales_credit import inputs


@pytest.mark.parametrize(
    "variant",
    [
        "valid",
        "duplicate",
        "truncated",
        "permission",
        "ambiguous_period",
        "wrong_order",
        "changed_invoice",
        "missing_refunds",
    ],
)
async def test_support_reads_fail_closed_and_are_bounded(variant):
    data = inputs()
    support = data["support"]
    now = datetime.now(timezone.utc)
    for refund in support["refunds"].values():
        refund["observed_at"] = now.isoformat()
    evidence = {
        "sections": {
            "posting_documents": [support["invoice"]],
            "linked_documents": support["linked_documents"],
            "invoice_applications": support["applications"],
            "gl": {"20": support["invoice_gl"]},
        }
    }
    config = SimpleNamespace(
        enabled=True,
        mapping_json={
            "sales_credit_profile": data["review"]["sales_credit_profile"],
            "solidus_refund_step_id": "refund-step",
            "reference_field": "otherrefnum",
        },
    )
    if variant == "missing_refunds":
        config.mapping_json.pop("solidus_refund_step_id")
    credit = deepcopy(support["reference_credit"])
    credit["item"] = {"items": credit.pop("line_items")}
    payloads = {
        "/record/v1/currency/1": support["currency"],
        "/record/v1/discountItem/50": support["item"],
        "/record/v1/creditMemo/30": credit,
        "/record/v1/accountingPeriod/171": support["period"],
        "/record/v1/invoice/20": support["invoice"],
    }
    if variant == "changed_invoice":
        payloads["/record/v1/invoice/20"] = {**support["invoice"], "lastModifiedDate": "changed"}

    class Reader:
        calls = 0

        async def request(self, method, path, params=None, body=None):
            self.calls += 1
            assert method == "GET" or path == "/query/v1/suiteql"
            if variant == "permission":
                raise PermissionError("Connected role cannot read")
            if path in payloads:
                return deepcopy(payloads[path])
            q = body["q"]
            if "SELECT DISTINCT" in q:
                rows = [{"id": "old-credit"}] if variant == "duplicate" else []
                return {"items": rows, "count": len(rows), "totalResults": len(rows), "hasMore": variant == "truncated"}
            rows = support["reference_gl"]["rows"] if "transactionaccountingline" in q else [{"id": "171"}]
            if "accountingperiod" in q and variant == "ambiguous_period":
                rows.append({"id": "172"})
            return {"items": deepcopy(rows), "count": len(rows), "totalResults": len(rows), "hasMore": False}

    reader = Reader()

    @asynccontextmanager
    async def connected(*args, **kwargs):
        assert args[1:4] == ("tenant", "connection", "123")
        assert kwargs["max_api_calls"] == 10
        yield reader

    order = AsyncMock(return_value={"orders": [{"record_id": "999" if variant == "wrong_order" else "90"}]})
    source_refunds = AsyncMock(return_value=support["refunds"]["source"])
    target_refunds = AsyncMock(return_value=support["refunds"]["target"])
    with (
        patch("app.services.transaction_ops.state_service.get_config", AsyncMock(return_value=config)),
        patch("app.services.transaction_ops.netsuite_reader.authenticated_reader", connected),
        patch("app.services.transaction_ops.netsuite_reader.read_netsuite_order", order),
        patch("app.services.transaction_ops.refund_reader.read_solidus_refunds", source_refunds),
        patch("app.services.transaction_ops.netsuite_refunds.read_netsuite_refunds", target_refunds),
    ):
        call = collect_support(None, "tenant", data["source"], data["report"], data["review"], evidence, now=now)
        if variant in {"permission", "ambiguous_period", "wrong_order", "changed_invoice", "missing_refunds"}:
            with pytest.raises((ValueError, PermissionError)):
                await call
        else:
            result = await call
            if variant == "valid":
                assert result["refunds"] == support["refunds"]
                assert result["invoice"] == support["invoice"]
                source_refunds.assert_awaited_once_with(None, "tenant", "refund-step", data["source"]["number"])
            else:
                assert result is None
                source_refunds.assert_not_awaited()
    assert reader.calls <= 8


@pytest.mark.parametrize(
    "variant",
    [
        "valid",
        "wrong_tenant",
        "changed_payload",
        "wrong_account",
        "case_resolved",
        "profile_changed",
        "source_changed",
        "gl_changed",
        "gl_reordered",
        "new_duplicate",
    ],
)
async def test_approval_rebuilds_candidate_from_current_scope_and_evidence(variant):
    data = inputs()
    now = datetime.now(timezone.utc)
    data["now"] = now
    data["support"]["observed_at"] = now.isoformat()
    for refund in data["support"]["refunds"].values():
        refund["observed_at"] = now.isoformat()
    data["review"]["native_mcp_connector_id"] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    p = build_candidate(**data)
    name, body = tool_inputs(p)
    review, source, support = (deepcopy(data[k]) for k in ("review", "source", "support"))
    case = SimpleNamespace(
        status="open",
        order_reference=p["order_reference"],
        id="case",
        scope_json=p["scope"],
        latest_report_json=data["report"],
    )
    connector = SimpleNamespace(server_url="https://123.suitetalk.api.netsuite.com/services/mcp")
    tenant = "tenant"
    if variant == "wrong_tenant":
        tenant = "another-tenant"
    if variant == "changed_payload":
        body["data"] = json.dumps({**p["proposed_fields"], "autoApply": True})
    if variant == "wrong_account":
        connector.server_url = "https://456.suitetalk.api.netsuite.com/services/mcp"
    if variant == "case_resolved":
        case.status = "reconciled"
    if variant == "profile_changed":
        review["sales_credit_profile"]["item_id"] = "999"
    if variant == "source_changed":
        source["total"] = "102"
    if variant == "gl_changed":
        support["invoice_gl"]["rows"][1]["account"] = "999"
    if variant == "gl_reordered":
        support["invoice_gl"]["rows"].reverse()
    if variant == "new_duplicate":
        support["duplicates"]["rows"] = [{"id": "31"}]
    db = AsyncMock()
    db.scalar.return_value = case
    with (
        patch("app.services.mcp_connector_service.get_mcp_connector", AsyncMock(return_value=connector)),
        patch("app.services.transaction_ops.accounting_review.accounting_context", AsyncMock(return_value=review)),
        patch("app.services.transaction_ops.tax_correction.refresh_source", AsyncMock(return_value=source)),
        patch(
            "app.services.transaction_ops.accounting_evidence.collect_accounting_evidence", AsyncMock(return_value={})
        ),
        patch("app.services.transaction_ops.commercial_credits.collect_commercial_credits", AsyncMock()),
        patch("app.services.transaction_ops.sales_credit.collect_support", AsyncMock(return_value=support)),
    ):
        if variant in {"valid", "gl_reordered"}:
            await validate_approved(db, tenant, name, body, p)
        else:
            with pytest.raises(ValueError):
                await validate_approved(db, tenant, name, body, p)
