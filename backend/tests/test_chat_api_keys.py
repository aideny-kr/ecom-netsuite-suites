"""Tests for Chat API Keys — ~15 tests."""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chat_api_key_service import authenticate_key, create_key
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pro_tenant(db: AsyncSession):
    return await create_test_tenant(db, name="API Corp", plan="pro")


@pytest_asyncio.fixture
async def pro_admin(db: AsyncSession, pro_tenant):
    user, _ = await create_test_user(db, pro_tenant, role_name="admin")
    return user, make_auth_headers(user)


@pytest_asyncio.fixture
async def pro_readonly(db: AsyncSession, pro_tenant):
    user, _ = await create_test_user(db, pro_tenant, role_name="readonly")
    return user, make_auth_headers(user)


@pytest_asyncio.fixture
async def pro_tenant_b(db: AsyncSession):
    return await create_test_tenant(db, name="Other API Corp", plan="pro")


@pytest_asyncio.fixture
async def pro_admin_b(db: AsyncSession, pro_tenant_b):
    user, _ = await create_test_user(db, pro_tenant_b, role_name="admin")
    return user, make_auth_headers(user)


# ---------------------------------------------------------------------------
# Key Creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_key_returns_raw_key(client: AsyncClient, pro_admin):
    _, headers = pro_admin
    resp = await client.post(
        "/api/v1/chat-api-keys",
        json={"name": "My Key", "scopes": ["chat"]},
        headers=headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["raw_key"].startswith("ck_")
    assert data["name"] == "My Key"
    assert data["key_prefix"].startswith("ck_")


@pytest.mark.asyncio
async def test_create_key_with_custom_rate_limit(client: AsyncClient, pro_admin):
    _, headers = pro_admin
    resp = await client.post(
        "/api/v1/chat-api-keys",
        json={"name": "Rate Limited", "scopes": ["chat"], "rate_limit_per_minute": 30},
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["rate_limit_per_minute"] == 30


@pytest.mark.asyncio
async def test_list_keys(client: AsyncClient, pro_admin):
    _, headers = pro_admin
    await client.post("/api/v1/chat-api-keys", json={"name": "Key 1"}, headers=headers)
    await client.post("/api/v1/chat-api-keys", json={"name": "Key 2"}, headers=headers)

    resp = await client.get("/api/v1/chat-api-keys", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 2
    # Ensure no hashes leaked
    for key in resp.json():
        assert "key_hash" not in key
        assert "raw_key" not in key


# ---------------------------------------------------------------------------
# Key Authentication (service-level)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticate_valid_key(db: AsyncSession, pro_tenant, pro_admin):
    user, _ = pro_admin
    api_key, raw_key = await create_key(db, pro_tenant.id, "Test Key", ["chat"], user.id)
    await db.flush()

    tenant_id, scopes = await authenticate_key(db, raw_key)
    assert tenant_id == pro_tenant.id
    assert "chat" in scopes


@pytest.mark.asyncio
async def test_authenticate_invalid_key(db: AsyncSession):
    with pytest.raises(ValueError, match="Invalid API key"):
        await authenticate_key(db, "ck_invalidkeythatdoesnotexist")


@pytest.mark.asyncio
async def test_authenticate_revoked_key(db: AsyncSession, pro_tenant, pro_admin):
    user, _ = pro_admin
    api_key, raw_key = await create_key(db, pro_tenant.id, "Revoked Key", ["chat"], user.id)
    api_key.is_active = False
    await db.flush()

    with pytest.raises(ValueError, match="revoked"):
        await authenticate_key(db, raw_key)


@pytest.mark.asyncio
async def test_authenticate_expired_key(db: AsyncSession, pro_tenant, pro_admin):
    user, _ = pro_admin
    api_key, raw_key = await create_key(
        db,
        pro_tenant.id,
        "Expired Key",
        ["chat"],
        user.id,
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    await db.flush()

    with pytest.raises(ValueError, match="expired"):
        await authenticate_key(db, raw_key)


# ---------------------------------------------------------------------------
# Key Revocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_key(client: AsyncClient, pro_admin):
    _, headers = pro_admin
    create_resp = await client.post(
        "/api/v1/chat-api-keys",
        json={"name": "To Revoke"},
        headers=headers,
    )
    key_id = create_resp.json()["id"]

    delete_resp = await client.delete(f"/api/v1/chat-api-keys/{key_id}", headers=headers)
    assert delete_resp.status_code == 204


# ---------------------------------------------------------------------------
# Tenant Isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_isolation_keys(client: AsyncClient, pro_admin, pro_admin_b):
    _, headers_a = pro_admin
    _, headers_b = pro_admin_b

    create_resp = await client.post(
        "/api/v1/chat-api-keys",
        json={"name": "Tenant A Key"},
        headers=headers_a,
    )
    key_id = create_resp.json()["id"]

    # Tenant B cannot revoke Tenant A's key
    resp = await client.delete(f"/api/v1/chat-api-keys/{key_id}", headers=headers_b)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readonly_cannot_create_key(client: AsyncClient, pro_readonly):
    _, headers = pro_readonly
    resp = await client.post(
        "/api/v1/chat-api-keys",
        json={"name": "Not Allowed"},
        headers=headers,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Audit Events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_events_for_key_lifecycle(client: AsyncClient, pro_admin, db: AsyncSession):
    user, headers = pro_admin
    create_resp = await client.post(
        "/api/v1/chat-api-keys",
        json={"name": "Audited Key"},
        headers=headers,
    )
    key_id = create_resp.json()["id"]
    await client.delete(f"/api/v1/chat-api-keys/{key_id}", headers=headers)

    from sqlalchemy import select

    from app.models.audit import AuditEvent

    result = await db.execute(
        select(AuditEvent).where(
            AuditEvent.tenant_id == user.tenant_id,
            AuditEvent.category == "chat_api",
        )
    )
    events = result.scalars().all()
    actions = [e.action for e in events]
    assert "chat_api.key_created" in actions
    assert "chat_api.key_revoked" in actions


# ---------------------------------------------------------------------------
# Integration: API Key Auth Endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_chat_missing_key(client: AsyncClient):
    resp = await client.post(
        "/api/v1/integration/chat",
        json={"message": "Hello"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_integration_chat_invalid_key(client: AsyncClient):
    resp = await client.post(
        "/api/v1/integration/chat",
        json={"message": "Hello"},
        headers={"X-API-Key": "ck_invalidkey"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_integration_chat_rejects_key_without_chat_scope(client: AsyncClient, pro_admin):
    _, headers = pro_admin
    key_resp = await client.post(
        "/api/v1/chat-api-keys",
        json={"name": "No Chat Scope", "scopes": ["workspace.read"]},
        headers=headers,
    )
    assert key_resp.status_code == 201
    raw_key = key_resp.json()["raw_key"]

    resp = await client.post(
        "/api/v1/integration/chat",
        json={"message": "Hello"},
        headers={"X-API-Key": raw_key},
    )
    assert resp.status_code == 403
    assert "chat" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Free Plan Entitlement Gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_free_plan_cannot_create_key(client: AsyncClient, admin_user):
    _, headers = admin_user
    resp = await client.post(
        "/api/v1/chat-api-keys",
        json={"name": "Not Allowed"},
        headers=headers,
    )
    assert resp.status_code == 403
