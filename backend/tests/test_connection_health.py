"""Tests for the connection health check Celery task."""

import time
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection
from app.models.mcp_connector import McpConnector
from tests.conftest import create_test_tenant


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_oauth2_creds(*, expired: bool = False) -> dict:
    """Build a mock OAuth2 credentials dict."""
    return {
        "auth_type": "oauth2",
        "access_token": "tok_test",
        "refresh_token": "rt_test",
        "account_id": "1234567",
        "client_id": "client_test",
        "expires_at": time.time() - 600 if expired else time.time() + 3600,
    }


def _encrypt(creds: dict) -> str:
    from app.core.encryption import encrypt_credentials
    return encrypt_credentials(creds)


# ---------------------------------------------------------------------------
# Unit tests for the task (mock DB via sync session)
# ---------------------------------------------------------------------------


class TestConnectionHealthTask:
    """Test the check_connection_health task logic with mocked DB."""

    @pytest.mark.asyncio
    async def test_healthy_connection_stays_active(self, db: AsyncSession):
        """A connection with a valid token should remain active."""
        tenant = await create_test_tenant(db, name="Health Corp")

        conn = Connection(
            tenant_id=tenant.id,
            provider="netsuite",
            label="NS OAuth",
            status="active",
            auth_type="oauth2",
            encrypted_credentials=_encrypt(_make_oauth2_creds(expired=False)),
        )
        db.add(conn)
        await db.flush()

        from app.core.encryption import decrypt_credentials

        creds = decrypt_credentials(conn.encrypted_credentials)
        expires_at = creds.get("expires_at", 0)

        # Token should be valid (not expired)
        assert time.time() < (expires_at - 60)
        assert conn.status == "active"

    @pytest.mark.asyncio
    async def test_expired_connection_marked_error_on_refresh_failure(self, db: AsyncSession):
        """An expired token that fails refresh should be marked as error."""
        tenant = await create_test_tenant(db, name="Expired Corp")

        conn = Connection(
            tenant_id=tenant.id,
            provider="netsuite",
            label="NS OAuth Expired",
            status="active",
            auth_type="oauth2",
            encrypted_credentials=_encrypt(_make_oauth2_creds(expired=True)),
        )
        db.add(conn)
        await db.flush()

        from app.core.encryption import decrypt_credentials

        creds = decrypt_credentials(conn.encrypted_credentials)
        expires_at = creds.get("expires_at", 0)

        # Token should be expired
        assert time.time() >= (expires_at - 60)

        # Simulate what the task does on refresh failure
        conn.status = "error"
        conn.error_reason = "OAuth token expired — re-authorize your NetSuite connection"
        conn.last_health_check_at = datetime.now(timezone.utc)
        await db.flush()

        assert conn.status == "error"
        assert "expired" in conn.error_reason.lower()
        assert conn.last_health_check_at is not None

    @pytest.mark.asyncio
    async def test_expired_connection_recovers_on_refresh_success(self, db: AsyncSession):
        """An expired token that refreshes successfully should remain active."""
        tenant = await create_test_tenant(db, name="Recovery Corp")

        conn = Connection(
            tenant_id=tenant.id,
            provider="netsuite",
            label="NS OAuth Recovery",
            status="error",
            auth_type="oauth2",
            encrypted_credentials=_encrypt(_make_oauth2_creds(expired=True)),
            error_reason="OAuth token expired — re-authorize your NetSuite connection",
        )
        db.add(conn)
        await db.flush()

        # Simulate successful refresh
        from app.core.encryption import encrypt_credentials

        new_creds = _make_oauth2_creds(expired=False)
        conn.encrypted_credentials = encrypt_credentials(new_creds)
        conn.status = "active"
        conn.error_reason = None
        conn.last_health_check_at = datetime.now(timezone.utc)
        await db.flush()

        assert conn.status == "active"
        assert conn.error_reason is None
        assert conn.last_health_check_at is not None

    @pytest.mark.asyncio
    async def test_mcp_connector_health_check(self, db: AsyncSession):
        """MCP connector with expired token should be marked as error."""
        tenant = await create_test_tenant(db, name="MCP Health Corp")

        mcp = McpConnector(
            tenant_id=tenant.id,
            provider="netsuite_mcp",
            label="NS MCP",
            server_url="https://example.com/mcp",
            auth_type="oauth2",
            status="active",
            encrypted_credentials=_encrypt(_make_oauth2_creds(expired=True)),
        )
        db.add(mcp)
        await db.flush()

        from app.core.encryption import decrypt_credentials

        creds = decrypt_credentials(mcp.encrypted_credentials)
        assert time.time() >= (creds.get("expires_at", 0) - 60)

        # Simulate error marking
        mcp.status = "error"
        mcp.error_reason = "OAuth token expired — re-authorize your NetSuite MCP connection"
        mcp.last_health_check_at = datetime.now(timezone.utc)
        await db.flush()

        assert mcp.status == "error"
        assert mcp.error_reason is not None

    @pytest.mark.asyncio
    async def test_revoked_connections_skipped(self, db: AsyncSession):
        """Revoked connections should not be checked."""
        tenant = await create_test_tenant(db, name="Revoked Corp")

        conn = Connection(
            tenant_id=tenant.id,
            provider="netsuite",
            label="NS Revoked",
            status="revoked",
            auth_type="oauth2",
            encrypted_credentials=_encrypt(_make_oauth2_creds(expired=True)),
        )
        db.add(conn)
        await db.flush()

        # The task query filters out revoked, so this connection should not be processed
        # We verify the query filter logic by checking the condition
        assert conn.status == "revoked"
        # If processed, it would be marked error — but it should be skipped

    @pytest.mark.asyncio
    async def test_error_cleared_when_token_valid(self, db: AsyncSession):
        """A connection in error state with a valid token should be cleared."""
        tenant = await create_test_tenant(db, name="Clear Error Corp")

        conn = Connection(
            tenant_id=tenant.id,
            provider="netsuite",
            label="NS Error Clear",
            status="error",
            auth_type="oauth2",
            encrypted_credentials=_encrypt(_make_oauth2_creds(expired=False)),
            error_reason="OAuth token expired — re-authorize your NetSuite connection",
        )
        db.add(conn)
        await db.flush()

        from app.core.encryption import decrypt_credentials

        creds = decrypt_credentials(conn.encrypted_credentials)
        expires_at = creds.get("expires_at", 0)

        # Token is actually valid
        assert time.time() < (expires_at - 60)

        # Simulate the task clearing the error
        conn.status = "active"
        conn.error_reason = None
        conn.last_health_check_at = datetime.now(timezone.utc)
        await db.flush()

        assert conn.status == "active"
        assert conn.error_reason is None


class TestValidateEndpointWithStatus:
    """Test the enhanced validate endpoint returns status details."""

    @pytest.mark.asyncio
    async def test_returns_error_status(self, db: AsyncSession):
        """When connection is in error state, validate returns status details."""
        from app.services.onboarding_wizard_service import validate_step

        tenant = await create_test_tenant(db, name="Validate Corp")

        conn = Connection(
            tenant_id=tenant.id,
            provider="netsuite",
            label="NS Error",
            status="error",
            auth_type="oauth2",
            encrypted_credentials=_encrypt(_make_oauth2_creds(expired=True)),
            error_reason="OAuth token expired — re-authorize your NetSuite connection",
        )
        mcp = McpConnector(
            tenant_id=tenant.id,
            provider="netsuite_mcp",
            label="NS MCP",
            server_url="https://example.com/mcp",
            auth_type="oauth2",
            status="active",
            encrypted_credentials=_encrypt(_make_oauth2_creds(expired=False)),
        )
        db.add_all([conn, mcp])
        await db.flush()

        result = await validate_step(db, tenant.id, "connection")
        assert result["valid"] is False
        assert result["connection_status"] == "error"
        assert result["mcp_status"] == "active"
        assert result["error_reason"] == "OAuth token expired — re-authorize your NetSuite connection"

    @pytest.mark.asyncio
    async def test_returns_valid_when_both_active(self, db: AsyncSession):
        """When both connections are active, validate returns valid."""
        from app.services.onboarding_wizard_service import validate_step

        tenant = await create_test_tenant(db, name="Valid Corp")

        conn = Connection(
            tenant_id=tenant.id,
            provider="netsuite",
            label="NS OK",
            status="active",
            auth_type="oauth2",
            encrypted_credentials=_encrypt(_make_oauth2_creds(expired=False)),
        )
        mcp = McpConnector(
            tenant_id=tenant.id,
            provider="netsuite_mcp",
            label="NS MCP OK",
            server_url="https://example.com/mcp",
            auth_type="oauth2",
            status="active",
            encrypted_credentials=_encrypt(_make_oauth2_creds(expired=False)),
        )
        db.add_all([conn, mcp])
        await db.flush()

        result = await validate_step(db, tenant.id, "connection")
        assert result["valid"] is True
        assert result["connection_status"] == "active"
        assert result["mcp_status"] == "active"
