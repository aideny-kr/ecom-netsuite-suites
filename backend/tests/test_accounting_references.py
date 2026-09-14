from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.mcp.tools import accounting_reference as tool
from app.mcp.tools.transaction_ops_tools import _ToolError
from app.services.transaction_ops import accounting_references as mod

URL = "https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2248474.html"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.html",
        "https://docs.oracle.com.evil.test/a.html",
        "https://user@docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/a.html",
        "https://docs.oracle.com:8443/en/cloud/saas/netsuite/ns-online-help/a.html",
        "https://docs.oracle.com/other.html",
        None,
    ],
)
def test_reference_reader_rejects_unapproved_hosts_and_paths(url):
    assert mod.official_url(url) is None


@pytest.mark.asyncio
async def test_only_fixed_queries_and_official_sources_are_used(monkeypatch):
    from app.mcp.tools import web_search

    search = AsyncMock(
        return_value={
            "results": [
                {"url": "http://127.0.0.1/private", "title": "bad"},
                {"url": URL + "?tracking=unused", "title": "Discount items"},
                {"url": URL, "title": "Duplicate"},
            ]
        }
    )
    read = AsyncMock(return_value={"excerpt": "Product reference", "document_sha256": "abc"})
    monkeypatch.setattr(web_search, "execute", search)
    monkeypatch.setattr(mod, "_read", read)
    result = await mod.research("invoice_discounts")
    assert search.await_args.args == (
        {
            "query": "site:docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/ " + mod.TOPICS["invoice_discounts"],
            "max_results": 5,
        },
    )
    read.assert_awaited_once_with(URL)
    assert len(result["sources"]) == 1
    assert result["sources"][0]["evidence_kind"] == "live_document_excerpt"
    assert result["status"] == "references_found"


@pytest.mark.asyncio
async def test_search_snippet_never_claimed_as_document_read(monkeypatch):
    from app.mcp.tools import web_search

    monkeypatch.setattr(
        web_search, "execute", AsyncMock(return_value={"results": [{"url": URL, "snippet": "summary"}]})
    )
    monkeypatch.setattr(mod, "_read", AsyncMock(side_effect=httpx.ConnectError("offline")))
    result = await mod.research("invoice_discounts")
    assert result["sources"][0]["evidence_kind"] == "search_snippet_only"
    assert "document_sha256" not in result["sources"][0]


@pytest.mark.asyncio
async def test_unavailable_research_does_not_manufacture_references(monkeypatch):
    from app.mcp.tools import web_search

    monkeypatch.setattr(web_search, "execute", AsyncMock(side_effect=TimeoutError))
    result = await mod.research("credit_memos")
    assert result["status"] == "reference_unavailable"
    assert result["sources"] == []


@pytest.mark.parametrize("available", [True, False])
async def test_rest_transform_uses_current_rest_document_not_soap_search_result(monkeypatch, available):
    from app.mcp.tools import web_search

    search = AsyncMock(side_effect=AssertionError("Known REST documentation should not be rediscovered"))
    read = AsyncMock(return_value={"excerpt": "REST transformation", "document_sha256": "hash"})
    if not available:
        read.side_effect = httpx.ConnectError("offline")
    monkeypatch.setattr(web_search, "execute", search)
    monkeypatch.setattr(mod, "_read", read)
    result = await mod.research("invoice_transform")
    search.assert_not_awaited()
    read.assert_awaited_once_with(mod._MAINTAINED["invoice_transform"][0]["url"])
    assert result["sources"][0]["product_surface"] == "REST Web Services"
    assert result["query"] is None
    assert result["status"] == ("references_found" if available else "reference_unavailable")
    if not available:
        assert result["sources"][0]["evidence_kind"] == "document_unavailable"
        assert "document_sha256" not in result["sources"][0]


@pytest.mark.asyncio
async def test_case_authorization_precedes_public_research(monkeypatch):
    research = AsyncMock()
    monkeypatch.setattr(tool, "research", research)
    monkeypatch.setattr(tool, "_authorize", AsyncMock(side_effect=_ToolError("permission_denied")))
    assert not (await tool.execute({"case_id": str(uuid4()), "topic": "invoice_discounts"}))["success"]
    research.assert_not_awaited()


@pytest.mark.asyncio
async def test_case_identity_cache_budget_and_audit(monkeypatch):
    from app.services import audit_service
    from app.services.transaction_ops import case_service

    tenant, case_id, actor = uuid4(), uuid4(), SimpleNamespace(id=uuid4())
    db = SimpleNamespace(info={})
    get_case = AsyncMock(return_value=SimpleNamespace(id=case_id))
    monkeypatch.setattr(tool, "_authorize", AsyncMock(return_value=(db, tenant, actor)))
    monkeypatch.setattr(case_service, "get_case", get_case)
    research = AsyncMock(return_value={"status": "references_found", "sources": [{"url": URL}]})
    monkeypatch.setattr(tool, "research", research)
    audit = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    monkeypatch.setattr(audit_service, "log_event", audit)
    context = {"correlation_id": "test"}
    params = {"case_id": str(case_id), "topic": "invoice_discounts"}
    first = await tool.execute(params, context=context)
    repeated = await tool.execute(params, context=context)
    assert repeated["reused"] and repeated["audit_id"] == first["audit_id"]
    research.assert_awaited_once_with("invoice_discounts")
    get_case.assert_awaited_with(db, tenant, case_id)
    assert audit.await_args.kwargs["actor_id"] == actor.id
    assert audit.await_args.kwargs["resource_id"] == str(case_id)
    assert (await tool.execute({**params, "topic": "credit_memos"}, context=context))["success"]
    assert not (await tool.execute({**params, "topic": "refunds"}, context=context))["success"]
    assert research.await_count == 2


@pytest.mark.asyncio
async def test_arbitrary_customer_text_cannot_enter_public_query(monkeypatch):
    authorize = AsyncMock()
    monkeypatch.setattr(tool, "_authorize", authorize)
    result = await tool.execute({"case_id": str(uuid4()), "topic": "customer@email.test invoice123"})
    assert result["success"] is False
    authorize.assert_not_awaited()
