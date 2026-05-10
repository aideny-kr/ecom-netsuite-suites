"""Tests for the suitecloud CLI per-run credential seeder."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import encrypt_credentials
from app.models.connection import Connection
from app.services.workspace.suitecloud_auth_seeder import (
    AuthSeederError,
    seed_credentials_for_run,
)


@pytest_asyncio.fixture
async def seeded_netsuite_connection(db: AsyncSession, tenant_a):
    """Create an active OAuth2 NetSuite connection with a fresh access token."""
    creds = {
        "auth_type": "oauth2",
        "access_token": "live_access_tok",
        "refresh_token": "live_refresh_tok",
        "expires_at": time.time() + 3600,  # fresh — get_valid_token short-circuits
        "account_id": "1234567",
        "client_id": "test_client_id_abc",
    }
    conn = Connection(
        tenant_id=tenant_a.id,
        provider="netsuite",
        label="Test NS",
        status="active",
        auth_type="oauth2",
        encrypted_credentials=encrypt_credentials(creds),
    )
    db.add(conn)
    await db.flush()
    return conn, creds


@pytest.mark.asyncio
async def test_seeder_writes_credential_file(
    db: AsyncSession,
    tmp_path: Path,
    seeded_netsuite_connection,
    tenant_a,
) -> None:
    _conn, creds = seeded_netsuite_connection
    cred_path = await seed_credentials_for_run(
        db=db,
        tenant_id=tenant_a.id,
        auth_root=tmp_path,
        project_id="ws-1",
    )
    assert cred_path.exists()
    assert cred_path.parent.parts[-2:] == (".suitecloud-sdk", "credentials")
    assert cred_path.name == "ws-1.json"
    payload = json.loads(cred_path.read_text())
    assert payload["accountId"] == creds["account_id"]
    assert payload["authType"] == "oauth2"
    assert payload["oauth2"]["clientId"] == creds["client_id"]
    assert payload["oauth2"]["accessToken"] == creds["access_token"]
    assert payload["oauth2"]["refreshToken"] == creds["refresh_token"]
    # Restrictive permissions: 0o600
    assert (cred_path.stat().st_mode & 0o777) == 0o600


@pytest.mark.asyncio
async def test_seeder_raises_when_no_connection(
    db: AsyncSession,
    tmp_path: Path,
) -> None:
    """No active NetSuite connection for tenant → AuthSeederError."""
    with pytest.raises(AuthSeederError, match="no active NetSuite connection"):
        await seed_credentials_for_run(
            db=db,
            tenant_id=uuid.uuid4(),
            auth_root=tmp_path,
            project_id="ws-1",
        )


@pytest.mark.asyncio
async def test_seeder_raises_when_token_refresh_fails(
    db: AsyncSession,
    tmp_path: Path,
    seeded_netsuite_connection,
    tenant_a,
) -> None:
    """get_valid_token returning None → AuthSeederError (refresh failed)."""
    with patch(
        "app.services.workspace.suitecloud_auth_seeder.get_valid_token",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(AuthSeederError, match="valid NetSuite access token"):
            await seed_credentials_for_run(
                db=db,
                tenant_id=tenant_a.id,
                auth_root=tmp_path,
                project_id="ws-1",
            )


@pytest.mark.asyncio
async def test_seeder_uses_refreshed_credentials(
    db: AsyncSession,
    tmp_path: Path,
    seeded_netsuite_connection,
    tenant_a,
) -> None:
    """When get_valid_token rotates encrypted_credentials in place, the seeder
    must re-decrypt so the file reflects the refreshed access_token / refresh_token /
    expires_at — not the stale pre-refresh values.
    """
    refreshed_creds = {
        "auth_type": "oauth2",
        "access_token": "REFRESHED_TOKEN",
        "refresh_token": "NEW_REFRESH",
        "expires_at": time.time() + 3600,
        "account_id": "1234567",
        "client_id": "test_client_id_abc",
    }

    async def _fake_get_valid(_db_arg, connection_arg):
        connection_arg.encrypted_credentials = encrypt_credentials(refreshed_creds)
        return "REFRESHED_TOKEN"

    with patch(
        "app.services.workspace.suitecloud_auth_seeder.get_valid_token",
        new=_fake_get_valid,
    ):
        cred_path = await seed_credentials_for_run(
            db=db,
            tenant_id=tenant_a.id,
            auth_root=tmp_path,
            project_id="ws-1",
        )
    payload = json.loads(cred_path.read_text())
    assert payload["oauth2"]["accessToken"] == "REFRESHED_TOKEN"
    assert payload["oauth2"]["refreshToken"] == "NEW_REFRESH"
    assert payload["oauth2"]["tokenExpiry"] == refreshed_creds["expires_at"]


@pytest.mark.asyncio
async def test_seeder_ignores_non_netsuite_connections(
    db: AsyncSession,
    tmp_path: Path,
    tenant_a,
) -> None:
    """Tenant has a Stripe connection but no NetSuite — seeder MUST raise,
    not silently seed Stripe creds into the suitecloud CLI credential file
    (which would then get sent as an OAuth bearer token to NetSuite).
    """
    db.add(
        Connection(
            tenant_id=tenant_a.id,
            provider="stripe",
            label="Test Stripe",
            status="active",
            auth_type="api_key",
            encrypted_credentials=encrypt_credentials({"api_key": "sk_test_x"}),
        )
    )
    await db.flush()
    with pytest.raises(AuthSeederError, match="no active NetSuite connection"):
        await seed_credentials_for_run(
            db=db,
            tenant_id=tenant_a.id,
            auth_root=tmp_path,
            project_id="ws-1",
        )
