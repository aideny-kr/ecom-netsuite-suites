"""Security hardening tests covering findings F1-F12 from SECURITY_VERIFICATION.md."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limit import reset_rate_limits
from app.core.security import create_access_token, decode_token
from app.core.token_denylist import reset_denylist, revoke_token
from app.models.audit import AuditEvent
from app.models.tenant import Tenant
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers


@pytest.fixture(autouse=True)
def _cleanup_rate_limits():
    """Reset rate limit and denylist state between tests."""
    reset_rate_limits()
    reset_denylist()
    yield
    reset_rate_limits()
    reset_denylist()


# ---------------------------------------------------------------------------
# F1 — Password Complexity
# ---------------------------------------------------------------------------


class TestPasswordComplexity:
    """F1: Password must contain uppercase, digit, and special character."""

    async def test_password_no_digit(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "tenant_name": "No Digit Corp",
                "tenant_slug": f"nd-{uuid.uuid4().hex[:8]}",
                "email": "a@nodigit.com",
                "password": "Abcdefgh!",
                "full_name": "Admin",
            },
        )
        assert resp.status_code == 422

    async def test_password_no_special(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "tenant_name": "No Special Corp",
                "tenant_slug": f"ns-{uuid.uuid4().hex[:8]}",
                "email": "a@nospecial.com",
                "password": "Abcdefgh1",
                "full_name": "Admin",
            },
        )
        assert resp.status_code == 422

    async def test_password_no_uppercase(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "tenant_name": "No Upper Corp",
                "tenant_slug": f"nu-{uuid.uuid4().hex[:8]}",
                "email": "a@noupper.com",
                "password": "abcdefgh1!",
                "full_name": "Admin",
            },
        )
        assert resp.status_code == 422

    async def test_valid_complex_password(self, client: AsyncClient, db: AsyncSession):
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "tenant_name": "Complex Corp",
                "tenant_slug": f"cp-{uuid.uuid4().hex[:8]}",
                "email": "a@complex.com",
                "password": "Abcdefgh1!",
                "full_name": "Admin",
            },
        )
        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# F2 — Login Rate Limiting
# ---------------------------------------------------------------------------


class TestLoginRateLimit:
    """F2: 10 login attempts per minute per IP, then 429."""

    async def test_rate_limit_blocks_after_10(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Rate Corp")
        await create_test_user(db, tenant, email="rate@test.com")
        await db.commit()

        for i in range(10):
            await client.post(
                "/api/v1/auth/login",
                json={"email": "rate@test.com", "password": "wrong"},
            )

        # 11th attempt should be rate-limited
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "rate@test.com", "password": "wrong"},
        )
        assert resp.status_code == 429


# ---------------------------------------------------------------------------
# F3 — Refresh Token as HttpOnly Cookie
# ---------------------------------------------------------------------------


class TestRefreshTokenCookie:
    """F3: Refresh token should be in HttpOnly cookie, not JSON body."""

    async def test_login_sets_cookie_no_body(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Cookie Corp")
        user, password = await create_test_user(db, tenant, email="cookie@test.com")
        await db.commit()

        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "cookie@test.com", "password": password},
        )
        assert resp.status_code == 200
        # refresh_token should be in cookie
        assert "refresh_token" in resp.cookies
        # refresh_token in body should be empty
        body = resp.json()
        assert body.get("refresh_token", "") == ""

    async def test_register_sets_cookie(self, client: AsyncClient, db: AsyncSession):
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "tenant_name": "CookieReg Corp",
                "tenant_slug": f"cr-{uuid.uuid4().hex[:8]}",
                "email": "cookiereg@test.com",
                "password": "Securepass1!",
                "full_name": "Admin",
            },
        )
        assert resp.status_code == 201
        assert "refresh_token" in resp.cookies


# ---------------------------------------------------------------------------
# F4 — JWT Denylist (Token Revocation)
# ---------------------------------------------------------------------------


class TestJWTDenylist:
    """F4: Revoked JTI results in token being rejected."""

    async def test_revoked_token_returns_401(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Denylist Corp")
        user, _ = await create_test_user(db, tenant, email="deny@test.com")
        await db.flush()

        token_data = {"sub": str(user.id), "tenant_id": str(user.tenant_id)}
        access_token = create_access_token(token_data)

        # Decode to get JTI, then revoke it
        payload = decode_token(access_token)
        assert payload is not None
        jti = payload["jti"]
        revoke_token(jti, payload["exp"])

        # Token should now be rejected
        headers = {"Authorization": f"Bearer {access_token}"}
        resp = await client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# F5 — Logout Endpoint
# ---------------------------------------------------------------------------


class TestLogout:
    """F5: POST /auth/logout revokes the token."""

    async def test_logout_revokes_token(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Logout Corp")
        user, password = await create_test_user(db, tenant, email="logout@test.com")
        await db.commit()

        # Login
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "logout@test.com", "password": password},
        )
        assert login_resp.status_code == 200
        access_token = login_resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {access_token}"}

        # Logout
        logout_resp = await client.post("/api/v1/auth/logout", headers=headers)
        assert logout_resp.status_code == 204

        # Subsequent request with same token should fail
        me_resp = await client.get("/api/v1/auth/me", headers=headers)
        assert me_resp.status_code == 401

    async def test_logout_creates_audit_event(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="LogoutAudit Corp")
        user, password = await create_test_user(db, tenant, email="logoutaudit@test.com")
        await db.commit()

        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "logoutaudit@test.com", "password": password},
        )
        access_token = login_resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {access_token}"}

        await client.post("/api/v1/auth/logout", headers=headers)

        result = await db.execute(
            select(AuditEvent).where(AuditEvent.action == "user.logout").order_by(AuditEvent.id.desc())
        )
        event = result.scalars().first()
        assert event is not None
        assert event.category == "auth"


# ---------------------------------------------------------------------------
# F8 — Audit Failed Login Attempts
# ---------------------------------------------------------------------------


class TestAuditFailedLogin:
    """F8: Failed login attempts are recorded in audit_events."""

    async def test_failed_login_creates_audit_event(self, client: AsyncClient, db: AsyncSession):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "nonexist@test.com", "password": "wrong"},
        )
        assert resp.status_code == 401

        result = await db.execute(
            select(AuditEvent).where(AuditEvent.action == "user.login_failed").order_by(AuditEvent.id.desc())
        )
        event = result.scalars().first()
        assert event is not None
        assert event.status == "denied"
        assert event.payload is not None
        assert event.payload["email"] == "nonexist@test.com"
        assert "ip" in event.payload


# ---------------------------------------------------------------------------
# F11 — Trial Plan Expiry
# ---------------------------------------------------------------------------


class TestTrialExpiry:
    """F11: Expired free-plan tenant gets 403."""

    async def test_expired_plan_blocks_access(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Expired Corp", plan="free")
        user, _ = await create_test_user(db, tenant, email="expired@test.com")
        # Set plan_expires_at to the past
        await db.execute(
            update(Tenant)
            .where(Tenant.id == tenant.id)
            .values(plan_expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        )
        await db.flush()

        headers = make_auth_headers(user)
        resp = await client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 403
        assert "expired" in resp.json()["detail"].lower()

    async def test_active_plan_allows_access(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Active Corp", plan="free")
        user, _ = await create_test_user(db, tenant, email="active@test.com")
        # plan_expires_at is in the future (default from factory)
        await db.flush()

        headers = make_auth_headers(user)
        resp = await client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# F12 — Deactivated Tenant Blocked
# ---------------------------------------------------------------------------


class TestDeactivatedTenant:
    """F12: Deactivated tenant is blocked on all authenticated endpoints."""

    async def test_deactivated_tenant_blocks_access(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="Deactivated Corp")
        user, _ = await create_test_user(db, tenant, email="deactivated@test.com")
        # Deactivate the tenant
        await db.execute(update(Tenant).where(Tenant.id == tenant.id).values(is_active=False))
        await db.flush()

        headers = make_auth_headers(user)
        resp = await client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 403
        assert "deactivated" in resp.json()["detail"].lower()

    async def test_deactivated_tenant_blocks_login(self, client: AsyncClient, db: AsyncSession):
        tenant = await create_test_tenant(db, name="DeactLogin Corp")
        user, password = await create_test_user(db, tenant, email="deactlogin@test.com")
        await db.execute(update(Tenant).where(Tenant.id == tenant.id).values(is_active=False))
        await db.commit()

        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "deactlogin@test.com", "password": password},
        )
        # Should fail with generic auth error (same as invalid password)
        assert resp.status_code == 401
