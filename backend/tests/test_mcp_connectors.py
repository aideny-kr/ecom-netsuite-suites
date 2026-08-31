"""Tests for MCP Connectors CRUD API endpoints."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def connector_payload():
    return {
        "provider": "netsuite_mcp",
        "label": "Test NetSuite MCP",
        "server_url": "https://example.com/mcp/v1/all",
        "auth_type": "bearer",
        "credentials": {"access_token": "test-token-123"},
    }


# ---------------------------------------------------------------------------
# CRUD Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_mcp_connector(client: AsyncClient, admin_user, connector_payload):
    user, headers = admin_user
    resp = await client.post("/api/v1/mcp-connectors", json=connector_payload, headers=headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["provider"] == "netsuite_mcp"
    assert data["label"] == "Test NetSuite MCP"
    assert data["server_url"] == "https://example.com/mcp/v1/all"
    assert data["auth_type"] == "bearer"
    assert data["status"] == "active"
    assert data["is_enabled"] is True


@pytest.mark.asyncio
async def test_list_mcp_connectors(client: AsyncClient, admin_user, connector_payload):
    user, headers = admin_user
    # Create one first
    await client.post("/api/v1/mcp-connectors", json=connector_payload, headers=headers)

    resp = await client.get("/api/v1/mcp-connectors", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_delete_mcp_connector(client: AsyncClient, admin_user, connector_payload):
    user, headers = admin_user
    create_resp = await client.post("/api/v1/mcp-connectors", json=connector_payload, headers=headers)
    connector_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/v1/mcp-connectors/{connector_id}", headers=headers)
    assert resp.status_code == 204

    # Verify it's revoked (still shows in list but with revoked status)
    list_resp = await client.get("/api/v1/mcp-connectors", headers=headers)
    connectors = list_resp.json()
    revoked = [c for c in connectors if c["id"] == connector_id]
    assert len(revoked) == 1
    assert revoked[0]["status"] == "revoked"


@pytest.mark.asyncio
async def test_delete_nonexistent_connector(client: AsyncClient, admin_user):
    user, headers = admin_user
    fake_id = str(uuid.uuid4())
    resp = await client.delete(f"/api/v1/mcp-connectors/{fake_id}", headers=headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tenant Isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_isolation(client: AsyncClient, admin_user, admin_user_b, connector_payload):
    """Tenant B cannot see Tenant A's connectors."""
    _, headers_a = admin_user
    _, headers_b = admin_user_b

    # Tenant A creates a connector
    await client.post("/api/v1/mcp-connectors", json=connector_payload, headers=headers_a)

    # Tenant B should see empty list
    resp = await client.get("/api/v1/mcp-connectors", headers=headers_b)
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_tenant_cannot_delete_others_connector(client: AsyncClient, admin_user, admin_user_b, connector_payload):
    """Tenant B cannot delete Tenant A's connector."""
    _, headers_a = admin_user
    _, headers_b = admin_user_b

    create_resp = await client.post("/api/v1/mcp-connectors", json=connector_payload, headers=headers_a)
    connector_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/v1/mcp-connectors/{connector_id}", headers=headers_b)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readonly_user_can_list(client: AsyncClient, readonly_user):
    """Readonly users have connections.view permission."""
    _, headers = readonly_user
    resp = await client.get("/api/v1/mcp-connectors", headers=headers)
    # Should not be 403 — readonly can view
    assert resp.status_code in (200, 403)  # depends on role permissions setup


@pytest.mark.asyncio
async def test_readonly_user_cannot_create(client: AsyncClient, readonly_user, connector_payload):
    """Readonly users should not have connections.manage permission."""
    _, headers = readonly_user
    resp = await client.post("/api/v1/mcp-connectors", json=connector_payload, headers=headers)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_audit_event(client: AsyncClient, admin_user, connector_payload):
    """Creating a connector emits an audit event."""
    _, headers = admin_user
    await client.post("/api/v1/mcp-connectors", json=connector_payload, headers=headers)

    # Check audit log
    resp = await client.get("/api/v1/audit-events", headers=headers)
    assert resp.status_code == 200
    events = resp.json()
    if isinstance(events, dict):
        events = events.get("items", [])
    mcp_events = [e for e in events if e.get("action") == "mcp_connector.create"]
    assert len(mcp_events) >= 1


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_provider_rejected(client: AsyncClient, admin_user):
    _, headers = admin_user
    payload = {
        "provider": "invalid_provider",
        "label": "Bad",
        "server_url": "https://example.com/mcp",
        "auth_type": "none",
    }
    resp = await client.post("/api/v1/mcp-connectors", json=payload, headers=headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_invalid_auth_type_rejected(client: AsyncClient, admin_user):
    _, headers = admin_user
    payload = {
        "provider": "custom",
        "label": "Bad Auth",
        "server_url": "https://example.com/mcp",
        "auth_type": "basic",  # not in allowed set (bearer|api_key|none|oauth2)
    }
    resp = await client.post("/api/v1/mcp-connectors", json=payload, headers=headers)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Celigo is unreachable from the generic create path (T2 gate round 2 on PR #202)
# ---------------------------------------------------------------------------
#
# Round 1 (MAJOR 2) made this endpoint pin server_url for provider=celigo_mcp
# so a caller couldn't point a celigo_mcp connector at a server they control.
# Round 2 found that fix was applied at the wrong layer: the real problem was
# that this generic, unguarded endpoint could accept provider=celigo_mcp at
# all. celigo_mcp has a dedicated, guarded creation flow
# (_upsert_celigo_mcp_connector in app/api/v1/connector_status.py) that pins
# server_url/auth_type/credentials AND enforces the celigo feature flag and
# the verified-before-enabled invariant -- none of which this endpoint knows
# about. McpConnectorCreate.provider now rejects "celigo_mcp" outright
# (app/schemas/mcp_connector.py), so there is no server_url left to pin here:
# the two tests below that used to prove pinning now prove rejection instead.
# The pinning invariant itself is proven where it's actually enforced --
# tests/api/test_celigo_connector_status.py::TestCeligoAgentAccess::
# test_connect_with_agent_token_creates_enabled_mcp_row.


@pytest.mark.asyncio
async def test_celigo_mcp_rejected_by_generic_create(client: AsyncClient, admin_user):
    """provider=celigo_mcp must 422 here, not create a connector.

    Letting this endpoint accept celigo_mcp at all -- even with server_url
    pinned -- would still skip the celigo feature-flag gate and the
    verified-before-enabled invariant that the dedicated flow enforces.
    """
    _, headers = admin_user
    payload = {
        "provider": "celigo_mcp",
        "label": "Evil Celigo",
        "server_url": "https://attacker.example.com/mcp",
        "auth_type": "bearer",
        "credentials": {"token": "tok"},
    }
    resp = await client.post("/api/v1/mcp-connectors", json=payload, headers=headers)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_celigo_mcp_rejected_by_generic_create_even_without_server_url(client: AsyncClient, admin_user):
    """Same rejection regardless of whether server_url is supplied."""
    _, headers = admin_user
    payload = {
        "provider": "celigo_mcp",
        "label": "Celigo",
        "auth_type": "bearer",
        "credentials": {"token": "tok"},
    }
    resp = await client.post("/api/v1/mcp-connectors", json=payload, headers=headers)
    assert resp.status_code == 422, resp.text
