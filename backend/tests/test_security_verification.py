"""
Security Verification Tests — ASVS v4.0 checklist coverage.

Covers the unchecked items in docs/Deferred/SECURITY_VERIFICATION.md.
Each test class maps to a specific ASVS section.
"""

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.encryption import encrypt_credentials
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
)
from app.models.canonical import Order
from app.models.connection import Connection
from app.models.tenant import Tenant
from app.models.user import User

from .conftest import create_test_tenant, create_test_user, make_auth_headers


# ---------------------------------------------------------------------------
# V2.1 — Password Security
# ---------------------------------------------------------------------------


class TestBcryptHashing:
    """V2.1.1 — Passwords hashed with bcrypt, cost >= 10."""

    async def test_hash_starts_with_bcrypt_prefix(self):
        h = hash_password("TestPass1!")
        assert h.startswith("$2b$"), f"Expected bcrypt prefix, got: {h[:10]}"

    async def test_bcrypt_cost_factor_at_least_10(self):
        h = hash_password("TestPass1!")
        # Format: $2b$<cost>$...
        cost = int(h.split("$")[2])
        assert cost >= 10, f"Bcrypt cost factor {cost} is below minimum 10"


class TestPasswordNotExposed:
    """V2.1.2 — hashed_password never returned in responses."""

    @pytest.mark.asyncio
    async def test_me_endpoint_excludes_hashed_password(
        self, client: AsyncClient, admin_user: tuple[User, dict]
    ):
        _, headers = admin_user
        resp = await client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert "hashed_password" not in body
        assert "password" not in body


# ---------------------------------------------------------------------------
# V2.2 — General Authenticator Security
# ---------------------------------------------------------------------------


class TestGenericAuthErrors:
    """V2.2.2 — Auth failures return identical messages (no user enumeration)."""

    @pytest.mark.asyncio
    async def test_identical_error_for_wrong_password_and_nonexistent_user(
        self, client: AsyncClient, db: AsyncSession, tenant_a: Tenant
    ):
        user, raw_pw = await create_test_user(
            db, tenant_a, email="real@test.com", password="ValidPass1!"
        )

        # Wrong password for existing user
        resp_wrong_pw = await client.post(
            "/api/v1/auth/login",
            json={"email": "real@test.com", "password": "WrongPass1!"},
        )

        # Nonexistent user
        resp_no_user = await client.post(
            "/api/v1/auth/login",
            json={"email": "ghost@test.com", "password": "WrongPass1!"},
        )

        assert resp_wrong_pw.status_code == 401
        assert resp_no_user.status_code == 401
        # Error messages must be identical
        assert resp_wrong_pw.json()["detail"] == resp_no_user.json()["detail"]


class TestDeactivatedUser:
    """V2.2.3 — Deactivated user (is_active=False) cannot login or use tokens."""

    @pytest.mark.asyncio
    async def test_deactivated_user_cannot_login(
        self, client: AsyncClient, db: AsyncSession, tenant_a: Tenant
    ):
        user, raw_pw = await create_test_user(
            db, tenant_a, email="deactivated@test.com", password="ValidPass1!"
        )
        # Deactivate the user
        user.is_active = False
        await db.flush()

        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "deactivated@test.com", "password": "ValidPass1!"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_deactivated_user_token_rejected(
        self, client: AsyncClient, db: AsyncSession, tenant_a: Tenant
    ):
        user, _ = await create_test_user(db, tenant_a, password="ValidPass1!")
        headers = make_auth_headers(user)

        # Token works before deactivation
        resp = await client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 200

        # Deactivate the user
        user.is_active = False
        await db.flush()

        # Token should be rejected
        resp = await client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# V3.1 — Token Storage and Transmission
# ---------------------------------------------------------------------------


class TestTokenExpiry:
    """V3.1.1 — Expired access tokens are rejected.
    V3.1.2 — Access token cannot be used as refresh token.
    """

    async def test_expired_token_returns_none(self):
        # Create a token with exp in the past
        token_data = {"sub": str(uuid.uuid4()), "tenant_id": str(uuid.uuid4())}
        expired_token = create_access_token(token_data)

        # Manually forge an expired token
        from jose import jwt as jose_jwt

        payload = jose_jwt.decode(
            expired_token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        payload["exp"] = datetime.now(timezone.utc) - timedelta(minutes=5)
        forged = jose_jwt.encode(
            payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
        )

        result = decode_token(forged)
        assert result is None, "Expired token should decode to None"

    @pytest.mark.asyncio
    async def test_access_token_rejected_as_refresh(
        self, client: AsyncClient, db: AsyncSession, tenant_a: Tenant
    ):
        user, _ = await create_test_user(db, tenant_a, password="ValidPass1!")
        access_token = create_access_token(
            {"sub": str(user.id), "tenant_id": str(user.tenant_id)}
        )

        # Try to use access token as refresh token
        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": access_token},
        )
        assert resp.status_code in (401, 422)


# ---------------------------------------------------------------------------
# V3.3 — Token Content
# ---------------------------------------------------------------------------


class TestJWTContent:
    """V3.3.1 — JWT payload contains only sub, tenant_id, exp, type, jti.
    V3.3.2 — Algorithm allowlist prevents alg:none attack.
    """

    async def test_access_token_payload_fields(self):
        token = create_access_token(
            {"sub": "user-123", "tenant_id": "tenant-456"}
        )
        # Decode the middle segment (payload)
        parts = token.split(".")
        # Add padding
        padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))

        expected_keys = {"sub", "tenant_id", "exp", "type", "jti"}
        assert set(payload.keys()) == expected_keys, (
            f"JWT has extra/missing fields: {set(payload.keys())} != {expected_keys}"
        )
        assert payload["type"] == "access"
        assert payload["sub"] == "user-123"
        assert payload["tenant_id"] == "tenant-456"

    async def test_refresh_token_payload_fields(self):
        token = create_refresh_token(
            {"sub": "user-123", "tenant_id": "tenant-456"}
        )
        parts = token.split(".")
        padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))

        expected_keys = {"sub", "tenant_id", "exp", "type", "jti"}
        assert set(payload.keys()) == expected_keys
        assert payload["type"] == "refresh"

    async def test_alg_none_token_rejected(self):
        """Forged token with alg:none must be rejected."""
        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "none", "typ": "JWT"}).encode()
        ).rstrip(b"=")
        payload = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "sub": str(uuid.uuid4()),
                    "tenant_id": str(uuid.uuid4()),
                    "exp": (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp(),
                    "type": "access",
                    "jti": uuid.uuid4().hex,
                }
            ).encode()
        ).rstrip(b"=")
        forged_token = f"{header.decode()}.{payload.decode()}."

        result = decode_token(forged_token)
        assert result is None, "Token with alg:none should be rejected"


# ---------------------------------------------------------------------------
# V6.1 — Credential Encryption at Rest
# ---------------------------------------------------------------------------


class TestCredentialEncryption:
    """V6.1.1 — Credentials encrypted with Fernet in DB."""

    @pytest.mark.asyncio
    async def test_encrypted_credentials_is_fernet_blob(
        self, db: AsyncSession, tenant_a: Tenant, admin_user: tuple[User, dict]
    ):
        user, _ = admin_user
        creds = {"api_key": "sk-live-test123", "secret": "whsec_test"}
        encrypted = encrypt_credentials(creds)

        # Fernet tokens start with 'gAAAAA'
        assert encrypted.startswith("gAAAAA"), (
            f"Expected Fernet token prefix, got: {encrypted[:10]}"
        )

        # Create a connection directly to check DB storage
        conn = Connection(
            tenant_id=tenant_a.id,
            provider="shopify",
            label="Test Shop",
            status="active",
            encrypted_credentials=encrypted,
            encryption_key_version=1,
        )
        db.add(conn)
        await db.flush()

        # Query raw DB value
        result = await db.execute(
            text(
                "SELECT encrypted_credentials FROM connections WHERE id = :id"
            ).bindparams(id=conn.id)
        )
        raw = result.scalar_one()
        assert raw.startswith("gAAAAA"), "DB value should be Fernet-encrypted"


# ---------------------------------------------------------------------------
# V6.2 — Sensitive Data in API Responses
# ---------------------------------------------------------------------------


class TestSensitiveDataExclusion:
    """V6.2.1 — ConnectionResponse excludes encrypted_credentials."""

    @pytest.mark.asyncio
    async def test_list_connections_excludes_credentials(
        self, client: AsyncClient, db: AsyncSession, tenant_a: Tenant, admin_user: tuple[User, dict]
    ):
        user, headers = admin_user
        encrypted = encrypt_credentials({"api_key": "secret"})
        conn = Connection(
            tenant_id=tenant_a.id,
            provider="shopify",
            label="Test",
            status="active",
            encrypted_credentials=encrypted,
            encryption_key_version=1,
            created_by=user.id,
        )
        db.add(conn)
        await db.flush()

        resp = await client.get("/api/v1/connections", headers=headers)
        assert resp.status_code == 200
        for c in resp.json():
            assert "encrypted_credentials" not in c
            assert "credentials" not in c
            assert "api_key" not in c
            assert "secret" not in c


# ---------------------------------------------------------------------------
# V10.1 — Idempotency and Deduplication
# ---------------------------------------------------------------------------


class TestIdempotency:
    """V10.1.1 — dedupe_key unique constraint prevents duplicate inserts."""

    @pytest.mark.asyncio
    async def test_duplicate_dedupe_key_rejected(
        self, db: AsyncSession, tenant_a: Tenant
    ):
        """Inserting two orders with the same tenant_id + dedupe_key should fail."""
        order1 = Order(
            tenant_id=tenant_a.id,
            dedupe_key="shopify:order:12345",
            source="shopify",
            source_id="12345",
            order_number="ORD-001",
            currency="USD",
            total_amount=Decimal("99.99"),
            subtotal=Decimal("89.99"),
            tax_amount=Decimal("10.00"),
            discount_amount=Decimal("0.00"),
            status="paid",
        )
        db.add(order1)
        await db.flush()

        order2 = Order(
            tenant_id=tenant_a.id,
            dedupe_key="shopify:order:12345",  # same dedupe_key
            source="shopify",
            source_id="12345",
            order_number="ORD-001-dup",
            currency="USD",
            total_amount=Decimal("99.99"),
            subtotal=Decimal("89.99"),
            tax_amount=Decimal("10.00"),
            discount_amount=Decimal("0.00"),
            status="paid",
        )
        db.add(order2)
        with pytest.raises(Exception):  # IntegrityError from unique constraint
            await db.flush()


# ---------------------------------------------------------------------------
# V13.2 — CORS
# ---------------------------------------------------------------------------


class TestCORSConfiguration:
    """V13.2.1 — CORS rejects non-allowlisted origins.
    V13.2.2 — allow_credentials=True with restricted origins.
    """

    @pytest.mark.asyncio
    async def test_cors_rejects_unknown_origin(self, client: AsyncClient):
        resp = await client.options(
            "/api/v1/auth/me",
            headers={
                "Origin": "https://evil.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        # Should NOT echo back the evil origin
        allow_origin = resp.headers.get("access-control-allow-origin", "")
        assert allow_origin != "https://evil.com"
        assert allow_origin != "*"

    @pytest.mark.asyncio
    async def test_cors_allows_configured_origin(self, client: AsyncClient):
        configured_origin = settings.cors_origins_list[0]
        resp = await client.options(
            "/api/v1/auth/me",
            headers={
                "Origin": configured_origin,
                "Access-Control-Request-Method": "GET",
            },
        )
        allow_origin = resp.headers.get("access-control-allow-origin", "")
        assert allow_origin == configured_origin

    @pytest.mark.asyncio
    async def test_cors_allows_credentials(self, client: AsyncClient):
        configured_origin = settings.cors_origins_list[0]
        resp = await client.options(
            "/api/v1/auth/me",
            headers={
                "Origin": configured_origin,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.headers.get("access-control-allow-credentials") == "true"

    async def test_cors_origins_not_wildcard(self):
        """Production must not use wildcard origins with credentials."""
        for origin in settings.cors_origins_list:
            assert origin != "*", "CORS origins must not be wildcard when credentials are enabled"
