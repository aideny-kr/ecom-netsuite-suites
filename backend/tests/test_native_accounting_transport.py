from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.services.transaction_ops import native_accounting_transport as mod


@pytest.fixture
def auth(monkeypatch):
    tenant, connection = uuid4(), uuid4()
    db = AsyncMock()
    db.scalar.return_value = SimpleNamespace(encrypted_credentials="encrypted")
    monkeypatch.setattr(mod, "set_tenant_context", AsyncMock())
    monkeypatch.setattr(mod, "decrypt_credentials", lambda _: {"account_id": "123456_SB1"})
    refresh = AsyncMock(return_value="test-only-token")
    monkeypatch.setattr(mod, "get_valid_token", refresh)
    return db, tenant, connection, refresh


async def test_public_interface_cannot_apply_or_redirect_to_another_script(auth):
    db, tenant, connection, refresh = auth
    for action, params in [("apply", {}), ("capabilities", {"script": "other"}), ("capabilities", {"deploy": "other"})]:
        with pytest.raises(mod.NativeTransportError):
            await mod.request(db, tenant, connection, "123456-sb1", action, params)
    refresh.assert_not_awaited()
    db.scalar.assert_not_awaited()


async def test_fixed_host_scope_existing_oauth_and_no_retry(auth):
    db, tenant, connection, refresh = auth
    requests = []

    def handle(request):
        requests.append(request)
        assert request.url.host == "123456-sb1.restlets.api.netsuite.com"
        assert request.url.params["script"] == mod.SCRIPT and request.url.params["deploy"] == mod.DEPLOYMENT
        assert request.headers["Authorization"] == "Bearer test-only-token"
        assert request.method == "GET"
        return httpx.Response(200, json={"schema_version": 1, "success": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        assert (await mod.request(db, tenant, connection, "123456_SB1", "capabilities", {}, client=client))[
            "success"
        ] is True
    assert len(requests) == 1
    query = str(db.scalar.await_args.args[0])
    assert "tenant_id" in query and "provider" in query and "status" in query
    refresh.assert_awaited_once()


@pytest.mark.parametrize(
    "mode", ["redirect", "large", "duplicate", "nan", "bool_version", "bad_json", "timeout", "http_error"]
)
async def test_native_http_failures_never_retry_or_expose_body_credentials(auth, mode):
    db, tenant, connection, _ = auth
    requests = []

    def handle(request):
        requests.append(request)
        if mode == "redirect":
            return httpx.Response(302, headers={"Location": "https://other.invalid"})
        if mode == "large":
            return httpx.Response(200, content=b"x" * (mod.MAX_BYTES + 1))
        if mode == "duplicate":
            return httpx.Response(200, content=b'{"schema_version":1,"success":true,"success":false}')
        if mode == "nan":
            return httpx.Response(200, content=b'{"schema_version":1,"value":NaN}')
        if mode == "bool_version":
            return httpx.Response(200, json={"schema_version": True})
        if mode == "bad_json":
            return httpx.Response(200, content=b"test-only-token")
        if mode == "timeout":
            raise httpx.ReadTimeout("test-only-token")
        return httpx.Response(503, content=b"test-only-token")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(mod.NativeTransportError) as caught:
            await mod.request(db, tenant, connection, "123456-sb1", "capabilities", {}, client=client)
    assert "test-only-token" not in str(caught.value)
    assert len(requests) == 1


async def test_foreign_account_and_unavailable_connection_never_acquire_token(auth):
    db, tenant, connection, refresh = auth
    with pytest.raises(mod.NativeTransportError, match="account_mismatch"):
        await mod.request(db, tenant, connection, "999999", "capabilities", {})
    refresh.assert_not_awaited()
    db.scalar.return_value = None
    with pytest.raises(mod.NativeTransportError, match="connection_unavailable"):
        await mod.request(db, tenant, connection, "123456-sb1", "capabilities", {})
    refresh.assert_not_awaited()
