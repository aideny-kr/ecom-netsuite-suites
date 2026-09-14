from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.chat import record_metadata_service as mod


@pytest.mark.asyncio
@pytest.mark.parametrize("record_type", ["invoice", "salesOrder"])
async def test_native_metadata_uses_schema_accept_header(record_type):
    import httpx

    from app.services.transaction_ops.netsuite_reader import _Reader

    def handle(request):
        assert request.headers["Accept"] == "application/schema+json"
        assert request.url.path == f"/record/v1/metadata-catalog/{record_type}"
        return httpx.Response(200, json={"properties": {"discountRate": {"type": "number"}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        reader = _Reader(client, "https://example.invalid", "test-token", max_api_calls=1)
        result = await reader.request("GET", f"/record/v1/metadata-catalog/{record_type}")
        assert "discountRate" in result["properties"] and reader.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record_type,kind,native_type",
    [("invoice", "invoice_sales_adjustment", "invoice"), ("salesorder", "sales_order_source_alignment", "salesOrder")],
)
@pytest.mark.parametrize("variant", ["valid", "account", "tenant", "disabled", "malformed", "missing_field"])
async def test_scoped_native_schema_preserves_validation_and_audit(
    monkeypatch, variant, record_type, kind, native_type
):
    mod.clear_metadata_cache()
    tenant, connector_id = uuid4(), uuid4()
    p = {
        "tenant_id": str(tenant),
        "kind": kind,
        "record_type": record_type,
        "connector_id": str(connector_id),
        "connection_id": "rest",
        "case_id": "case",
        "scope": {"netsuite_account_id": "123"},
        "proposed_fields": {"discountItem": {"id": "50"}, "discountRate": -5},
    }
    connector = SimpleNamespace(
        status="active",
        is_enabled=variant != "disabled",
        server_url="https://999.suitetalk.api.netsuite.com"
        if variant == "account"
        else "https://123.suitetalk.api.netsuite.com",
    )
    get = AsyncMock(return_value=connector)
    monkeypatch.setattr("app.services.mcp_connector_service.get_mcp_connector", get)
    audit = AsyncMock()
    monkeypatch.setattr("app.services.audit_service.log_event", audit)
    schema = {"properties": {"discountItem": {"type": "object"}, "discountRate": {"type": "number"}}}
    if variant == "malformed":
        schema = {"properties": None}
    if variant == "missing_field":
        schema["properties"].pop("discountRate")
    request = AsyncMock(return_value=schema)

    @asynccontextmanager
    async def reader(db, actual_tenant, connection_id, account, **kwargs):
        assert (actual_tenant, connection_id, account, kwargs) == (tenant, "rest", "123", {"max_api_calls": 1})
        yield SimpleNamespace(request=request)

    monkeypatch.setattr("app.services.transaction_ops.netsuite_reader.authenticated_reader", reader)
    try:
        if variant != "valid":
            with pytest.raises(ValueError):
                await mod.prefetch_scoped_invoice_metadata(
                    None, uuid4() if variant == "tenant" else tenant, "actor", p, "correlation"
                )
            audit.assert_not_awaited()
            assert not mod._cache
        else:
            await mod.prefetch_scoped_invoice_metadata(None, tenant, "actor", p, "correlation")
            await mod.prefetch_scoped_invoice_metadata(None, tenant, "actor", p, "correlation")
            request.assert_awaited_once_with("GET", f"/record/v1/metadata-catalog/{native_type}")
            audit.assert_awaited_once()
            assert audit.call_args.kwargs["actor_id"] == "actor"
            meta = mod._cache[(str(connector_id), record_type)][1]
            assert meta.requirements_known is False
            assert meta.spec_for("discountRate").type == "number"
    finally:
        mod.clear_metadata_cache()
