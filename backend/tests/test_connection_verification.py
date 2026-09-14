"""Provider tests must perform bounded reads and preserve honest health state."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.core.encryption import encrypt_credentials
from app.models.mcp_connector import McpConnector
from app.services import connection_service, mcp_connector_service
from app.services.http_connector_service import ConnectorReadError


async def test_stripe_failure_and_recovery_are_persisted(client, admin_user, monkeypatch):
    _, headers = admin_user
    upstream = AsyncMock(side_effect=ConnectorReadError("authentication_failed"))
    monkeypatch.setattr("app.services.http_connector_service.read_json", upstream)
    created = await client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "provider": "stripe",
            "label": "Stripe",
            "credentials": {"api_key": "sk_test_private"},
        },
    )
    identifier = created.json()["id"]
    endpoint = f"/api/v1/connections/{identifier}/test"
    failed = (await client.post(endpoint, headers=headers)).json()
    assert failed["status"] == "error"
    assert "permission" in failed["message"].lower()
    row = next(c for c in (await client.get("/api/v1/connections", headers=headers)).json() if c["id"] == identifier)
    assert row["last_health_check_at"]
    assert row["error_reason"] == failed["message"]
    assert row["status"] == "error"
    upstream.side_effect = None
    upstream.return_value = {"object": "balance", "available": []}
    assert (await client.post(endpoint, headers=headers)).json()["status"] == "ok"
    args = upstream.call_args.args
    assert args[0] == {"base_url": "https://api.stripe.com/", "auth_type": "bearer", "token": "sk_test_private"}
    assert args[1] == "v1/balance"
    row = next(c for c in (await client.get("/api/v1/connections", headers=headers)).json() if c["id"] == identifier)
    assert row["status"] == "active" and row["error_reason"] is None
    assert "sk_test_private" not in str(row)


@pytest.mark.parametrize(
    "provider,payload,expected",
    [
        ("bigquery", {"kind": "bigquery#datasetList", "datasets": []}, "ok"),
        ("google_sheets", {"files": []}, "partial"),
    ],
)
async def test_native_google_routes_without_mcp_or_writes(db, admin_user, monkeypatch, provider, payload, expected):
    from app.services import connection_verification

    user, _ = admin_user
    row = McpConnector(
        tenant_id=user.tenant_id,
        provider=provider,
        label=provider,
        server_url="native://unused",
        auth_type="service_account",
        status="error",
        is_enabled=False,
        encrypted_credentials=encrypt_credentials({"service_account_json": {}, "project_id": "test-project"}),
        metadata_json={"shared_drive_id": "shared-drive"},
        discovered_tools=[],
    )
    db.add(row)
    await db.flush()
    token = AsyncMock(return_value="temporary-token")
    upstream = AsyncMock(return_value=payload)
    discovery = AsyncMock(side_effect=AssertionError("Native adapters are not MCP servers"))
    monkeypatch.setattr(connection_verification, "_google_access_token", token)
    monkeypatch.setattr("app.services.http_connector_service.read_json", upstream)
    monkeypatch.setattr("app.services.mcp_client_service.discover_tools", discovery)
    result = await mcp_connector_service.test_mcp_connector(db, row.id, user.tenant_id)
    assert result["status"] == expected
    assert row.status == "active" and row.error_reason is None and row.last_health_check_at
    assert row.is_enabled is False and row.discovered_tools == []
    discovery.assert_not_awaited()
    assert upstream.await_count == 1
    path = upstream.call_args.args[1]
    assert "maxResults=1" in path if provider == "bigquery" else "driveId=shared-drive" in path
    upstream.side_effect = ConnectorReadError("authentication_failed")
    assert (await mcp_connector_service.test_mcp_connector(db, row.id, user.tenant_id))["status"] == "error"
    assert row.status == "error" and row.error_reason


async def test_netsuite_read_success_does_not_probe_an_unrelated_file(db, admin_user, monkeypatch):
    user, _ = admin_user
    row = await connection_service.create_connection(
        db,
        user.tenant_id,
        "netsuite",
        "NS",
        {
            "auth_type": "oauth2",
            "account_id": "123456",
            "access_token": "private",
        },
    )
    query = AsyncMock(return_value={"rows": []})
    files = AsyncMock(side_effect=RuntimeError("file unavailable"))
    monkeypatch.setattr("app.services.netsuite_oauth_service.get_valid_token", AsyncMock(return_value="private"))
    monkeypatch.setattr("app.services.netsuite_client.execute_suiteql_via_rest", query)
    monkeypatch.setattr("app.services.netsuite_restlet_client.restlet_read_file", files)
    result = await connection_service.test_connection(db, row.id, user.tenant_id)
    assert result["status"] == "ok"
    files.assert_not_awaited()
    assert query.call_args.args[:2] == ("private", "123456")
    assert query.call_args.kwargs["timeout_seconds"] <= 25
    assert row.last_health_check_at and row.status == "active"


async def test_mcp_error_reason_and_timestamp_survive_list(client, admin_user, monkeypatch):
    _, headers = admin_user
    monkeypatch.setattr("app.services.mcp_client_service.discover_tools", AsyncMock(side_effect=RuntimeError("SECRET")))
    response = await client.post(
        "/api/v1/mcp-connectors",
        headers=headers,
        json={
            "provider": "custom",
            "label": "MCP",
            "server_url": "https://mcp.example/mcp",
            "auth_type": "none",
        },
    )
    identifier = response.json()["id"]
    row = next(c for c in (await client.get("/api/v1/mcp-connectors", headers=headers)).json() if c["id"] == identifier)
    assert row["last_health_check_at"] and row["error_reason"]
    assert "SECRET" not in str(row)


@pytest.mark.parametrize(
    "code,fragment",
    [
        ("authentication_failed", "permission"),
        ("rate_limited", "rate limit"),
        ("invalid_response", "unexpected"),
        ("transport_failed", "could not"),
    ],
)
async def test_stripe_reports_safe_provider_failures(db, admin_user, monkeypatch, code, fragment):
    user, _ = admin_user
    row = await connection_service.create_connection(db, user.tenant_id, "stripe", "Stripe", {"api_key": "private"})
    monkeypatch.setattr(
        "app.services.http_connector_service.read_json", AsyncMock(side_effect=ConnectorReadError(code))
    )
    result = await connection_service.test_connection(db, row.id, user.tenant_id)
    assert result["status"] == "error" and fragment in result["message"]
    assert row.status == "error" and "private" not in result["message"]


@pytest.mark.parametrize("status", ["revoked", "superseded"])
async def test_retired_and_foreign_native_credentials_are_never_used(db, admin_user, admin_user_b, monkeypatch, status):
    user, _ = admin_user
    upstream = AsyncMock()
    monkeypatch.setattr("app.services.http_connector_service.read_json", upstream)
    row = await connection_service.create_connection(db, user.tenant_id, "stripe", "Stripe", {"api_key": "private"})
    row.status = status
    assert (await connection_service.test_connection(db, row.id, user.tenant_id))["status"] == "error"
    assert (await connection_service.test_connection(db, row.id, admin_user_b[0].tenant_id))["status"] == "error"
    assert row.status == status
    upstream.assert_not_awaited()


async def test_netsuite_failure_never_returns_provider_body(db, admin_user, monkeypatch):
    user, _ = admin_user
    row = await connection_service.create_connection(
        db, user.tenant_id, "netsuite", "NS", {"auth_type": "oauth2", "account_id": "123456"}
    )
    monkeypatch.setattr("app.services.netsuite_oauth_service.get_valid_token", AsyncMock(return_value="private"))
    response = httpx.Response(403, request=httpx.Request("POST", "https://example.test"))
    monkeypatch.setattr(
        "app.services.netsuite_client.execute_suiteql_via_rest",
        AsyncMock(side_effect=httpx.HTTPStatusError("SECRET", request=response.request, response=response)),
    )
    result = await connection_service.test_connection(db, row.id, user.tenant_id)
    assert result["status"] == "error" and "permissions" in result["message"]
    assert "SECRET" not in str(result) and row.status == "error"


async def test_oauth1_uses_the_selected_connections_credentials(db, admin_user, monkeypatch):
    user, _ = admin_user
    await connection_service.create_connection(db, user.tenant_id, "netsuite", "Other", {"account_id": "111111"})
    saved = {"account_id": "222222_SB1", "consumer_key": "selected"}
    row = await connection_service.create_connection(db, user.tenant_id, "netsuite", "Selected", saved)
    signer = MagicMock(return_value={"Authorization": "signed"})
    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.build_oauth1_header", signer)
    remote = AsyncMock()
    remote.post.return_value = httpx.Response(
        200, json={"items": []}, request=httpx.Request("POST", "https://example.test")
    )
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = remote
    monkeypatch.setattr(connection_service.httpx, "AsyncClient", factory)
    assert (await connection_service.test_connection(db, row.id, user.tenant_id))["status"] == "ok"
    assert signer.call_args.args[0] == saved
    assert remote.post.call_args.args[0].startswith("https://222222-sb1.suitetalk.api.netsuite.com/")
    assert remote.post.call_args.kwargs["json"] == {"q": "SELECT id FROM transaction WHERE ROWNUM <= 1"}


async def test_google_refresh_pins_token_url_and_bounds_request(monkeypatch):
    from app.services.connection_verification import _google_access_token

    account = MagicMock(token="temporary")
    account.refresh.side_effect = lambda request: request(
        "https://oauth2.googleapis.com/token", method="POST", timeout=120
    )
    factory = MagicMock(return_value=account)
    request = MagicMock()
    monkeypatch.setattr("google.oauth2.service_account.Credentials.from_service_account_info", factory)
    monkeypatch.setattr("google.auth.transport.requests.Request", MagicMock(return_value=request))
    assert await _google_access_token({"token_uri": "https://untrusted.example/"}, ("readonly",)) == "temporary"
    assert factory.call_args.args[0]["token_uri"] == "https://oauth2.googleapis.com/token"
    assert request.call_args.kwargs["timeout"] == 10
    assert request.call_args.kwargs["allow_redirects"] is False
    account.refresh.side_effect = lambda request: request("https://untrusted.example/", method="POST")
    with pytest.raises(ValueError):
        await _google_access_token({}, ("readonly",))
    assert request.call_count == 1


async def test_sheets_reads_only_one_file_and_verifies_its_identity(monkeypatch):
    from app.services import connection_verification

    monkeypatch.setattr(connection_verification, "_google_access_token", AsyncMock(return_value="temporary"))
    upstream = AsyncMock(side_effect=[{"files": [{"id": "sheet-id"}]}, {"spreadsheetId": "sheet-id"}])
    monkeypatch.setattr("app.services.http_connector_service.read_json", upstream)
    result = await connection_verification.check_google("google_sheets", {"service_account_json": {}}, {})
    assert result["status"] == "ok" and upstream.await_count == 2
    assert upstream.call_args.args[1] == "v4/spreadsheets/sheet-id?fields=spreadsheetId"
    upstream.side_effect = [{"files": [{"id": "../other"}]}]
    with pytest.raises(ConnectorReadError):
        await connection_verification.check_google("google_sheets", {"service_account_json": {}}, {})
    assert upstream.await_count == 3


async def test_test_endpoint_requires_management_permission(client, admin_user, readonly_user, monkeypatch):
    _, headers = admin_user
    created = await client.post(
        "/api/v1/connections",
        headers=headers,
        json={
            "provider": "stripe",
            "label": "Stripe",
            "credentials": {"api_key": "private"},
        },
    )
    upstream = AsyncMock()
    monkeypatch.setattr("app.services.http_connector_service.read_json", upstream)
    endpoint = f"/api/v1/connections/{created.json()['id']}/test"
    assert (await client.post(endpoint, headers=readonly_user[1])).status_code == 403
    assert (await client.post(endpoint)).status_code == 401
    upstream.assert_not_awaited()
