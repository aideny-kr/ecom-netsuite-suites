"""Idle renewal uses the existing actor-scoped grant and shared rotation lock."""

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import decrypt_credentials, encrypt_credentials
from app.models.mcp_connector import McpConnector
from app.models.tenant import Tenant
from app.services import metabase_oauth_service as oauth
from app.workers.tasks.proactive_token_refresh import _refresh_metabase_one
from tests.test_metabase_oauth import ORIGIN, URL
from tests.test_metabase_oauth import connector as connector_fixture
from tests.test_metabase_oauth import provider as provider_fixture

connector = connector_fixture
provider = provider_fixture


async def credentials(db, connector, *, expires_in=-10):
    connector.encrypted_credentials = encrypt_credentials(
        {
            "oauth_provider": "metabase",
            "client_id": "client",
            "resource": URL,
            "token_endpoint": ORIGIN + "/oauth/token",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": time.time() + expires_in,
            "scope": " ".join(oauth.READ_SCOPES),
        }
    )
    connector.status, connector.is_enabled = "active", True
    await db.flush()


@pytest.mark.parametrize("expires_in", [300, -10])
async def test_rotates_before_expiry_and_while_idle(db, connector, provider, expires_in):
    await credentials(db, connector, expires_in=expires_in)
    now = datetime.now(timezone.utc)
    stats = {"refreshed": 0, "errors": 0}
    await _refresh_metabase_one(db, connector, stats, now)
    assert stats == {"refreshed": 1, "errors": 0}
    assert decrypt_credentials(connector.encrypted_credentials)["refresh_token"] == "secret-refresh"
    assert connector.last_health_check_at == now
    assert connector.is_enabled and connector.status == "active"
    assert len(provider[0]) == 1
    await _refresh_metabase_one(db, connector, stats, now)
    assert len(provider[0]) == 1


@pytest.mark.parametrize("status,enabled", [("revoked", True), ("superseded", True), ("active", False)])
async def test_never_refreshes_a_disabled_or_retired_connector(db, connector, provider, status, enabled):
    connector.status, connector.is_enabled = status, enabled
    await db.flush()
    stats = {"refreshed": 0, "errors": 0}
    await _refresh_metabase_one(db, connector, stats, datetime.now(timezone.utc))
    assert not provider[0]
    assert stats == {"refreshed": 0, "errors": 0}
    assert connector.status == status and connector.is_enabled == enabled


async def test_outage_keeps_credentials_then_recovers(db, connector, provider):
    await credentials(db, connector)
    before = connector.encrypted_credentials
    request = provider[1].side_effect
    provider[1].side_effect = oauth.OAuthError("Temporary outage", code="metabase_upstream_unavailable")
    stats = {"refreshed": 0, "errors": 0}
    with pytest.raises(oauth.OAuthError) as exc:
        await _refresh_metabase_one(db, connector, stats, datetime.now(timezone.utc))
    assert exc.value.transient
    assert connector.encrypted_credentials == before and connector.status == "active"
    provider[1].side_effect = request
    await _refresh_metabase_one(db, connector, stats, datetime.now(timezone.utc))
    assert stats["refreshed"] == 1


async def test_expired_grant_reports_reconnect_without_secrets_and_can_recover(db, connector, provider):
    await credentials(db, connector)
    before = connector.encrypted_credentials
    request = provider[1].side_effect
    provider[1].side_effect = oauth.OAuthError("Invalid grant: sensitive-provider-body")
    stats = {"refreshed": 0, "errors": 0}
    await _refresh_metabase_one(db, connector, stats, datetime.now(timezone.utc))
    assert stats == {"refreshed": 0, "errors": 1}
    assert connector.encrypted_credentials == before
    assert connector.status == "error" and "reconnection" in connector.error_reason
    assert "sensitive" not in connector.error_reason
    provider[1].side_effect = request
    await _refresh_metabase_one(db, connector, stats, datetime.now(timezone.utc))
    assert connector.status == "active" and connector.error_reason is None


@pytest.mark.parametrize("buffer", [59, 601, True, "600"])
async def test_refresh_buffer_rejects_invalid_values(db, connector, buffer):
    with pytest.raises(ValueError, match="invalid_refresh_buffer"):
        await oauth.get_token(connector, db, refresh_buffer_seconds=buffer)


@pytest.mark.parametrize("scenario", ["concurrent", "stale_disabled", "inactive_tenant"])
async def test_real_commits_serialize_rotation_and_do_not_revive_disabled_access(db, provider, monkeypatch, scenario):
    from app.core import database
    from app.workers.tasks.proactive_token_refresh import _refresh_metabase

    # A private local fixture with real COMMITs tests the lock across independent
    # sessions, unlike the ordinary outer-transaction/savepoint fixture.
    engine = db.bind.engine
    tenant_id, connector_id = uuid4(), uuid4()
    assert engine.url.host in {"localhost", "127.0.0.1"}
    creds = {
        "oauth_provider": "metabase",
        "client_id": "client",
        "resource": URL,
        "token_endpoint": ORIGIN + "/oauth/token",
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": time.time() - 10,
        "scope": " ".join(oauth.READ_SCOPES),
    }
    try:
        async with engine.begin() as conn:
            await conn.execute(
                Tenant.__table__.insert().values(
                    id=tenant_id,
                    name="OAuth test",
                    slug="oauth-test-" + tenant_id.hex,
                    is_active=scenario != "inactive_tenant",
                )
            )
            await conn.execute(
                McpConnector.__table__.insert().values(
                    id=connector_id,
                    tenant_id=tenant_id,
                    provider="custom",
                    label="OAuth test",
                    server_url=URL,
                    auth_type="oauth2",
                    status="active",
                    is_enabled=True,
                    encrypted_credentials=encrypt_credentials(creds),
                )
            )
        ready, loaded = asyncio.Event(), 0

        async def worker():
            nonlocal loaded
            async with AsyncSession(engine, expire_on_commit=False) as session:
                await database.set_tenant_context_session(session, str(tenant_id))
                row = await session.scalar(select(McpConnector).where(McpConnector.id == connector_id))
                loaded += 1
                if loaded == 2:
                    ready.set()
                await ready.wait()
                stats = {"refreshed": 0, "errors": 0}
                await _refresh_metabase_one(session, row, stats, datetime.now(timezone.utc))
                return stats

        if scenario == "concurrent":
            stats = await asyncio.gather(worker(), worker())
            assert sum(item["refreshed"] for item in stats) == 1
            assert len(provider[0]) == 1
        elif scenario == "stale_disabled":
            async with AsyncSession(engine, expire_on_commit=False) as session:
                row = await session.scalar(select(McpConnector).where(McpConnector.id == connector_id))
                async with engine.begin() as conn:
                    await conn.execute(
                        McpConnector.__table__.update().where(McpConnector.id == connector_id).values(is_enabled=False)
                    )
                stats = {"refreshed": 0, "errors": 0}
                await _refresh_metabase_one(session, row, stats, datetime.now(timezone.utc))
                assert not row.is_enabled and not provider[0]
        else:

            @asynccontextmanager
            async def factory(**kwargs):
                assert kwargs == {"pin_connection": True}
                async with AsyncSession(engine, expire_on_commit=False) as session:
                    yield session

            monkeypatch.setattr(database, "worker_async_session", factory)
            stats = {"checked": 0, "refreshed": 0, "errors": 0}
            await _refresh_metabase([(tenant_id, connector_id)], stats, datetime.now(timezone.utc))
            assert stats == {"checked": 1, "refreshed": 0, "errors": 0} and not provider[0]
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                delete(McpConnector).where(McpConnector.id == connector_id, McpConnector.tenant_id == tenant_id)
            )
            await conn.execute(delete(Tenant).where(Tenant.id == tenant_id))
