"""Tests for authentication flows: register, login, refresh, /me, tenant switching."""
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent
from app.models.tenant import Tenant
from app.models.user import User
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegister:
    async def test_register_success(self, client: AsyncClient, db: AsyncSession):
        slug = f"reg-{uuid.uuid4().hex[:8]}"
        resp = await client.post("/api/v1/auth/register", json={
            "tenant_name": "Reg Corp",
            "tenant_slug": slug,
            "email": "admin@regcorp.com",
            "password": "securepass123",
            "full_name": "Admin User",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"

    async def test_register_duplicate_slug(self, client: AsyncClient, db: AsyncSession):
        slug = f"dup-{uuid.uuid4().hex[:8]}"
        payload = {
            "tenant_name": "Dup Corp",
            "tenant_slug": slug,
            "email": "a@dup.com",
            "password": "securepass123",
            "full_name": "Admin",
        }
        resp1 = await client.post("/api/v1/auth/register", json=payload)
        assert resp1.status_code == 201

        payload["email"] = "b@dup.com"
        resp2 = await client.post("/api/v1/auth/register", json=payload)
        assert resp2.status_code == 400
        assert "slug" in resp2.json()["detail"].lower()

    async def test_register_creates_audit_event(self, client: AsyncClient, db: AsyncSession):
        slug = f"aud-{uuid.uuid4().hex[:8]}"
        resp = await client.post("/api/v1/auth/register", json={
            "tenant_name": "Audit Corp",
            "tenant_slug": slug,
            "email": "admin@auditcorp.com",
            "password": "securepass123",
            "full_name": "Admin",
        })
        assert resp.status_code == 201

        result = await db.execute(
            select(AuditEvent).where(AuditEvent.action == "tenant.register").order_by(AuditEvent.id.desc())
        )
        event = result.scalars().first()
        assert event is not None
        assert event.category == "auth"

    async def test_register_invalid_slug(self, client: AsyncClient):
        resp = await client.post("/api/v1/auth/register", json={
            "tenant_name": "Bad Slug Corp",
            "tenant_slug": "INVALID SLUG!",
            "email": "admin@bad.com",
            "password": "securepass123",
            "full_name": "Admin",
        })
        assert resp.status_code == 422

    async def test_register_short_password(self, client: AsyncClient):
        resp = await client.post("/api/v1/auth/register", json={
            "tenant_name": "Short Pass",
            "tenant_slug": f"sp-{uuid.uuid4().hex[:8]}",
            "email": "admin@short.com",
            "password": "short",
            "full_name": "Admin",
        })
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


class TestLogin:
    async def test_login_success(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Login Corp")
        user, password = await create_test_user(db, tenant, email="login@test.com")
        await db.commit()

        resp = await client.post("/api/v1/auth/login", json={
            "email": "login@test.com",
            "password": password,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data

    async def test_login_wrong_password(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Wrong PW Corp")
        await create_test_user(db, tenant, email="wrongpw@test.com")
        await db.commit()

        resp = await client.post("/api/v1/auth/login", json={
            "email": "wrongpw@test.com",
            "password": "wrongpassword",
        })
        assert resp.status_code == 401

    async def test_login_nonexistent_email(self, client: AsyncClient):
        resp = await client.post("/api/v1/auth/login", json={
            "email": "noexist@test.com",
            "password": "anything",
        })
        assert resp.status_code == 401

    async def test_login_creates_audit_event(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Login Audit Corp")
        user, password = await create_test_user(db, tenant, email="loginaudit@test.com")
        await db.commit()

        resp = await client.post("/api/v1/auth/login", json={
            "email": "loginaudit@test.com",
            "password": password,
        })
        assert resp.status_code == 200

        result = await db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "user.login",
                AuditEvent.tenant_id == tenant.id,
            )
        )
        event = result.scalars().first()
        assert event is not None
        assert event.category == "auth"


# ---------------------------------------------------------------------------
# Refresh Token
# ---------------------------------------------------------------------------


class TestRefresh:
    async def test_refresh_success(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Refresh Corp")
        user, password = await create_test_user(db, tenant, email="refresh@test.com")
        await db.commit()

        login_resp = await client.post("/api/v1/auth/login", json={
            "email": "refresh@test.com",
            "password": password,
        })
        refresh_token = login_resp.json()["refresh_token"]

        resp = await client.post("/api/v1/auth/refresh", json={
            "refresh_token": refresh_token,
        })
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    async def test_refresh_invalid_token(self, client: AsyncClient):
        resp = await client.post("/api/v1/auth/refresh", json={
            "refresh_token": "invalid-token",
        })
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /me
# ---------------------------------------------------------------------------


class TestMe:
    async def test_me_returns_profile(self, client: AsyncClient, admin_user):
        user, headers = admin_user
        resp = await client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(user.id)
        assert data["email"] == user.email
        assert data["full_name"] == user.full_name
        assert "admin" in data["roles"]
        assert "tenant_name" in data

    async def test_me_no_auth(self, client: AsyncClient):
        resp = await client.get("/api/v1/auth/me")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Tenant switching
# ---------------------------------------------------------------------------


class TestSwitchTenant:
    async def test_list_tenants(self, client: AsyncClient, db: AsyncSession):
        """User with accounts in multiple tenants sees all of them."""
        email = f"multi-{uuid.uuid4().hex[:6]}@test.com"
        tenant1 = await create_test_tenant(db, name="Multi Corp 1")
        tenant2 = await create_test_tenant(db, name="Multi Corp 2")
        user1, _ = await create_test_user(db, tenant1, email=email, role_name="admin")
        user2, _ = await create_test_user(db, tenant2, email=email, role_name="admin")
        await db.commit()

        headers = make_auth_headers(user1)
        resp = await client.get("/api/v1/auth/me/tenants", headers=headers)
        assert resp.status_code == 200
        tenant_ids = {t["id"] for t in resp.json()}
        assert str(tenant1.id) in tenant_ids
        assert str(tenant2.id) in tenant_ids

    async def test_switch_tenant_success(self, client: AsyncClient, db: AsyncSession):
        """User can switch to a different tenant they belong to."""
        email = f"switch-{uuid.uuid4().hex[:6]}@test.com"
        tenant1 = await create_test_tenant(db, name="Switch Corp 1")
        tenant2 = await create_test_tenant(db, name="Switch Corp 2")
        user1, _ = await create_test_user(db, tenant1, email=email, role_name="admin")
        user2, _ = await create_test_user(db, tenant2, email=email, role_name="admin")
        await db.commit()

        headers = make_auth_headers(user1)
        resp = await client.post("/api/v1/auth/switch-tenant", json={
            "tenant_id": str(tenant2.id),
        }, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data

    async def test_switch_tenant_no_account(self, client: AsyncClient, db: AsyncSession, admin_user, tenant_b):
        """User cannot switch to a tenant they don't have an account in."""
        user, headers = admin_user
        resp = await client.post("/api/v1/auth/switch-tenant", json={
            "tenant_id": str(tenant_b.id),
        }, headers=headers)
        assert resp.status_code == 403
