"""Metabase OAuth: real HTTP/tenant authorization around a stub provider."""

import time
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio

from app.core.encryption import decrypt_credentials, encrypt_credentials
from app.models.mcp_connector import McpConnector
from app.services import metabase_oauth_service as oauth

URL = "https://analytics.example.com/api/metabase-mcp"
ORIGIN = "https://analytics.example.com"
APP = "https://app.example.com"


class StateStore:
    def __init__(self):
        self.values = {}

    async def setex(self, key, ttl, value):
        assert ttl == 600
        self.values[key] = value

    async def get(self, key):
        return self.values.get(key)

    async def getdel(self, key):
        return self.values.pop(key, None)

    async def aclose(self):
        pass


@pytest.fixture
def provider(monkeypatch):
    calls = []

    async def request(url, *, method="GET", **kwargs):
        calls.append((url, method, kwargs))
        if "oauth-protected-resource" in url:
            return {"resource": URL, "authorization_servers": [ORIGIN]}
        if "oauth-authorization-server" in url:
            return {
                "issuer": ORIGIN,
                "authorization_endpoint": ORIGIN + "/oauth/authorize",
                "token_endpoint": ORIGIN + "/oauth/token",
                "registration_endpoint": ORIGIN + "/oauth/register",
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": list(oauth.READ_SCOPES),
            }
        if url.endswith("/register"):
            return {"client_id": "registered-client", "token_endpoint_auth_method": "none"}
        if url.endswith("/token"):
            return {
                "access_token": "secret-access",
                "refresh_token": "secret-refresh",
                "expires_in": 3600,
                "token_type": "Bearer",
            }
        raise AssertionError(url)

    mock = AsyncMock(side_effect=request)
    monkeypatch.setattr(oauth, "request_json", mock)
    store = StateStore()
    monkeypatch.setattr(oauth, "state_store", lambda: store)
    monkeypatch.setattr(oauth.settings, "CORS_ORIGINS", APP)
    monkeypatch.setattr(
        oauth.settings, "NETSUITE_OAUTH_REDIRECT_URI", "https://api.example.com/api/v1/connections/netsuite/callback"
    )
    discovery = AsyncMock(return_value=[{"name": "search", "description": "Search", "inputSchema": {}}])
    monkeypatch.setattr("app.services.mcp_client_service.discover_tools", discovery)
    return calls, mock, store, discovery


@pytest_asyncio.fixture
async def connector(db, admin_user):
    user, _ = admin_user
    row = McpConnector(
        tenant_id=user.tenant_id,
        provider="custom",
        label="Metabase",
        server_url=URL,
        auth_type="oauth2",
        status="error",
        is_enabled=False,
        metadata_json={"oauth_provider": "metabase", "setup_state": "awaiting_oauth_support"},
    )
    db.add(row)
    await db.flush()
    return row


async def begin(client, row, headers, origin=APP):
    return await client.post(
        f"/api/v1/mcp-connectors/{row.id}/metabase/authorize", headers=headers, json={"app_origin": origin}
    )


async def finish(client, row, headers, state, **extra):
    return await client.post(
        f"/api/v1/mcp-connectors/{row.id}/metabase/complete",
        headers=headers,
        json={"state": state, "code": "authorization-code", **extra},
    )


@pytest.mark.asyncio
async def test_sign_in_pkce_encrypted_credentials_discovery_and_replay(client, db, admin_user, connector, provider):
    _, headers = admin_user
    result = await begin(client, connector, headers)
    assert result.status_code == 200, result.text
    data = result.json()
    query = parse_qs(urlsplit(data["authorize_url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["resource"] == [URL]
    assert set(query["scope"][0].split()) == set(oauth.READ_SCOPES)
    assert not any("create" in s or "update" in s or "sql" in s for s in oauth.READ_SCOPES)
    assert connector.encrypted_credentials is None and not connector.is_enabled
    assert "code_verifier" not in str(data)
    assert "registered-client" not in str(provider[2].values)  # encrypted pending state
    callback = await client.get(
        "/api/v1/mcp-connectors/metabase/oauth/callback",
        params={"state": data["state"], "code": "</script><img src=x onerror=alert(1)>"},
    )
    assert callback.status_code == 200
    assert "</script><img" not in callback.text
    assert APP in callback.text and '"*"' not in callback.text
    assert callback.headers["cache-control"] == "no-store"
    assert connector.encrypted_credentials is None  # callback cannot install credentials
    done = await finish(client, connector, headers, data["state"])
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "ok"
    await db.refresh(connector)
    credentials = decrypt_credentials(connector.encrypted_credentials)
    assert credentials["access_token"] == "secret-access"
    assert credentials["refresh_token"] == "secret-refresh"
    assert connector.is_enabled and connector.status == "active"
    assert connector.metadata_json["setup_state"] == "connected"
    assert "secret-access" not in done.text
    assert (await finish(client, connector, headers, data["state"])).status_code == 400
    assert len([c for c in provider[0] if c[0].endswith("/token")]) == 1


@pytest.mark.asyncio
async def test_tenant_permission_origin_and_actor_binding(
    client, admin_user, admin_user_b, readonly_user, connector, provider
):
    _, headers = admin_user
    assert (await begin(client, connector, admin_user_b[1])).status_code == 404
    assert (await begin(client, connector, readonly_user[1])).status_code == 403
    assert (await begin(client, connector, headers, "https://evil.example")).status_code == 400
    state = (await begin(client, connector, headers)).json()["state"]
    assert (await finish(client, connector, admin_user_b[1], state)).status_code == 404
    # Service itself rejects a different actor, even in the owning tenant.
    pending = await oauth.load_state(state)
    pending["user_id"] = str(readonly_user[0].id)
    await provider[2].setex(oauth.state_key(state), 600, encrypt_credentials(pending))
    assert (await finish(client, connector, headers, state)).status_code == 400
    assert not any(c[0].endswith("/token") for c in provider[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoked", "server", "credentials", "cancel", "expired"])
async def test_stale_deleted_canceled_or_expired_flow_cannot_connect(
    client, db, admin_user, connector, provider, change
):
    headers = admin_user[1]
    state = (await begin(client, connector, headers)).json()["state"]
    if change == "revoked":
        connector.status = "revoked"
    elif change == "server":
        connector.server_url = "https://other.example/api/metabase-mcp"
    elif change == "credentials":
        connector.encrypted_credentials = encrypt_credentials({"replacement": True})
    elif change == "expired":
        provider[2].values.clear()
    await db.flush()
    result = await finish(
        client, connector, headers, state, **({"error": "access_denied"} if change == "cancel" else {})
    )
    assert result.status_code in (400, 404)
    assert not connector.is_enabled
    assert not any(c[0].endswith("/token") for c in provider[0])


@pytest.mark.asyncio
async def test_discovery_failure_preserves_tokens_and_test_recovers(client, db, admin_user, connector, provider):
    headers = admin_user[1]
    state = (await begin(client, connector, headers)).json()["state"]
    provider[3].side_effect = RuntimeError("sensitive provider payload")
    result = await finish(client, connector, headers, state)
    assert result.status_code == 200
    assert result.json()["status"] == "error"
    assert "sensitive" not in result.text
    assert not connector.is_enabled
    assert decrypt_credentials(connector.encrypted_credentials)["access_token"]
    provider[3].side_effect = None
    result = await client.post(f"/api/v1/mcp-connectors/{connector.id}/test", headers=headers)
    assert result.json()["status"] == "ok"
    assert connector.is_enabled and connector.metadata_json["setup_state"] == "connected"


@pytest.mark.asyncio
async def test_refresh_uses_metabase_rotates_and_does_not_use_netsuite(db, connector, provider):
    creds = {
        "oauth_provider": "metabase",
        "client_id": "client",
        "token_endpoint": ORIGIN + "/oauth/token",
        "resource": URL,
        "access_token": "expired",
        "expires_at": time.time() - 5,
        "refresh_token": "old-refresh",
        "scope": " ".join(oauth.READ_SCOPES),
    }
    connector.encrypted_credentials = encrypt_credentials(creds)
    connector.status = "active"
    connector.is_enabled = True
    await db.flush()
    from app.services.mcp_client_service import _get_oauth2_token

    assert await _get_oauth2_token(connector, db) == "secret-access"
    saved = decrypt_credentials(connector.encrypted_credentials)
    assert saved["refresh_token"] == "secret-refresh"
    request = provider[0][-1][2]["data"]
    assert request["grant_type"] == "refresh_token" and request["resource"] == URL
    count = provider[1].await_count
    assert await _get_oauth2_token(connector, db) == "secret-access"
    assert provider[1].await_count == count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("token_endpoint", "http://127.0.0.1/token"),
        ("registration_endpoint", "https://evil.example/register"),
        ("issuer", "https://evil.example"),
        ("code_challenge_methods_supported", ["plain"]),
    ],
)
async def test_discovery_rejects_unsafe_or_incompatible_metadata(client, admin_user, connector, provider, field, value):
    original = provider[1].side_effect

    async def tampered(url, **kwargs):
        result = await original(url, **kwargs)
        if "oauth-authorization-server" in url:
            result[field] = value
        return result

    provider[1].side_effect = tampered
    result = await begin(client, connector, admin_user[1])
    assert result.status_code == 400
    assert not any(c[1] == "POST" for c in provider[0])


@pytest.mark.asyncio
async def test_pending_oauth_cannot_pass_test_without_credentials(client, admin_user, connector, provider):
    result = await client.post(f"/api/v1/mcp-connectors/{connector.id}/test", headers=admin_user[1])
    assert result.json()["status"] == "error"
    assert "Connect with Metabase" in result.json()["message"]
    provider[3].assert_not_awaited()
    assert not connector.is_enabled


@pytest.mark.asyncio
async def test_refresh_failure_never_returns_expired_token_or_leaks_provider_errors(db, connector, provider):
    connector.encrypted_credentials = encrypt_credentials(
        {
            "oauth_provider": "metabase",
            "client_id": "client",
            "resource": URL,
            "token_endpoint": ORIGIN + "/oauth/token",
            "expires_at": 0,
            "access_token": "expired-secret",
            "refresh_token": "refresh-secret",
        }
    )
    await db.flush()
    provider[1].side_effect = oauth.OAuthError("Provider unavailable")
    from app.services.mcp_client_service import _build_headers

    with pytest.raises(RuntimeError, match="Metabase") as exc:
        await _build_headers(connector, db)
    assert "expired-secret" not in str(exc.value)
    assert "NetSuite" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides", [{"token_type": "MAC"}, {"scope": "mb:full"}, {"expires_in": -1}, {"access_token": ""}]
)
async def test_invalid_grant_is_not_saved(client, admin_user, connector, provider, overrides):
    state = (await begin(client, connector, admin_user[1])).json()["state"]
    provider[1].side_effect = None
    provider[1].return_value = {"access_token": "secret", "token_type": "Bearer", "expires_in": 3600, **overrides}
    result = await finish(client, connector, admin_user[1], state)
    assert result.status_code == 400
    assert connector.encrypted_credentials is None and not connector.is_enabled


@pytest.mark.asyncio
async def test_creating_metabase_oauth_stays_disabled_until_consent(client, admin_user, provider):
    result = await client.post(
        "/api/v1/mcp-connectors",
        headers=admin_user[1],
        json={
            "provider": "custom",
            "label": "Metabase",
            "server_url": URL,
            "auth_type": "oauth2",
        },
    )
    assert result.status_code == 201
    assert result.json()["status"] == "error"
    assert result.json()["is_enabled"] is False
    assert result.json()["metadata_json"]["setup_state"] == "authorization_required"
    provider[3].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body", [(302, b""), (400, b"secret-provider-error"), (200, b"x" * 65537), (200, b"[]"), (200, b"not-json")]
)
async def test_oauth_http_rejects_redirects_errors_oversized_and_invalid_json(monkeypatch, status, body):
    import httpx

    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, content=body, headers={"Location": "https://evil.example"})
    )
    monkeypatch.setattr(oauth, "PublicHTTPTransport", lambda endpoint: transport)
    with pytest.raises(oauth.OAuthError) as exc:
        await oauth.request_json(ORIGIN + "/oauth/token")
    assert "secret-provider-error" not in str(exc.value)
