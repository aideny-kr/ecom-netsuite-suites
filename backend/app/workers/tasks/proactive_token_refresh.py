"""Proactive token refresh — runs every 5 minutes.

Refreshes tokens BEFORE they expire, preventing the reactive-only pattern
where tokens go stale during idle periods. NetSuite uses its existing distributed
locks; Metabase shares the reactive reader's PostgreSQL rotation lock.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

REFRESH_BUFFER_SECONDS = 600  # Refresh if expiring within 10 minutes


@celery_app.task(name="tasks.proactive_token_refresh", queue="sync")
def proactive_token_refresh():
    """Proactively refresh OAuth tokens about to expire."""
    from app.core.config import settings
    from app.models.connection import Connection
    from app.models.mcp_connector import McpConnector
    from app.models.tenant import Tenant
    from app.workers.base_task import sync_engine

    now = datetime.now(timezone.utc)
    stats = {"checked": 0, "refreshed": 0, "errors": 0, "skipped_locked": 0}

    with Session(sync_engine) as db:
        # ── REST API Connections ──
        connections = (
            db.execute(
                select(Connection).where(
                    Connection.provider == "netsuite",
                    Connection.status.in_(["active", "error"]),
                )
            )
            .scalars()
            .all()
        )

        for conn in connections:
            stats["checked"] += 1
            _refresh_single(db, conn, "oauth_refresh", stats, now, settings)

        # ── MCP Connectors ──
        mcp_connectors = (
            db.execute(
                select(McpConnector).where(
                    McpConnector.provider == "netsuite_mcp",
                    McpConnector.auth_type == "oauth2",
                    McpConnector.status.in_(["active", "error"]),
                )
            )
            .scalars()
            .all()
        )

        for mcp in mcp_connectors:
            stats["checked"] += 1
            _refresh_single(db, mcp, "oauth_refresh:mcp", stats, now, settings)

        from app.services.metabase_oauth_service import is_metabase

        metabase = [
            (row.tenant_id, row.id)
            for row in db.scalars(
                select(McpConnector)
                .join(Tenant, Tenant.id == McpConnector.tenant_id)
                .where(
                    McpConnector.provider == "custom",
                    McpConnector.auth_type == "oauth2",
                    McpConnector.status.in_(["active", "error"]),
                    McpConnector.is_enabled.is_(True),
                    Tenant.is_active.is_(True),
                )
            )
            if is_metabase(row)
        ]
        db.commit()

    if metabase:
        asyncio.run(_refresh_metabase(metabase, stats, now))

    logger.info("proactive_token_refresh.completed", extra=stats)
    print(f"[proactive_token_refresh] {stats}", flush=True)
    return stats


async def _refresh_metabase(records, stats, now):
    from app.core.database import set_tenant_context_session, worker_async_session
    from app.models.mcp_connector import McpConnector
    from app.models.tenant import Tenant

    for tenant_id, connector_id in records:
        stats["checked"] += 1
        try:
            async with worker_async_session(pin_connection=True) as db:
                await set_tenant_context_session(db, str(tenant_id))
                row = await db.scalar(
                    select(McpConnector)
                    .join(Tenant, Tenant.id == McpConnector.tenant_id)
                    .where(
                        McpConnector.tenant_id == tenant_id,
                        McpConnector.id == connector_id,
                        Tenant.is_active.is_(True),
                    )
                )
                if row is not None:
                    await _refresh_metabase_one(db, row, stats, now)
        except Exception as exc:
            stats["errors"] += 1
            # Provider bodies and encrypted credentials must never enter logs.
            logger.warning(
                "proactive_token_refresh.metabase_failed",
                extra={"connector_id": str(connector_id), "error_type": type(exc).__name__},
            )


async def _refresh_metabase_one(db, connector, stats, now):
    from app.services.metabase_oauth_service import get_token, is_metabase

    await db.refresh(connector, with_for_update=True)
    if not is_metabase(connector) or not connector.is_enabled or connector.status not in ("active", "error"):
        return
    before = connector.encrypted_credentials
    token = await get_token(connector, db, refresh_buffer_seconds=REFRESH_BUFFER_SECONDS)
    await db.refresh(connector, with_for_update=True)
    # Recheck after the rotation lock: revocation/disable during the wait wins.
    if not connector.is_enabled or connector.status not in ("active", "error") or not is_metabase(connector):
        return
    if token is None:
        connector.last_health_check_at = now
        stats["errors"] += 1
        connector.status = "error"
        connector.error_reason = "Metabase authorization needs reconnection."
    elif before != connector.encrypted_credentials:
        connector.last_health_check_at = now
        stats["refreshed"] += 1
    if token and connector.error_reason == "Metabase authorization needs reconnection.":
        connector.status = "active"
        connector.error_reason = None
    await db.commit()


def _refresh_single(db, record, lock_prefix, stats, now, settings):
    """Refresh a single connection/connector if token is expiring soon."""
    from app.core.encryption import decrypt_credentials, encrypt_credentials
    from app.core.redis_lock import acquire_lock, release_lock

    try:
        if not record.encrypted_credentials:
            return

        creds = decrypt_credentials(record.encrypted_credentials)
        if creds.get("auth_type") != "oauth2":
            return

        expires_at = creds.get("expires_at", 0)
        if time.time() < (expires_at - REFRESH_BUFFER_SECONDS):
            return  # Still has >10 minutes — skip

        refresh_token = creds.get("refresh_token")
        account_id = creds.get("account_id")

        if not refresh_token or not account_id:
            return  # Can't refresh — health check will flag this

        # Always use stored per-connection client_id — each connection has its own
        # Integration Record in NetSuite with its own Client ID.
        client_id = creds.get("client_id", "")

        if not client_id:
            return

        lock_key = f"{lock_prefix}:{record.id}"
        from app.services import oauth_refresh_lock

        rest = lock_prefix == "oauth_refresh"
        owner = oauth_refresh_lock.acquire(lock_key) if rest else acquire_lock(lock_key, timeout=30)
        if not owner:
            stats["skipped_locked"] += 1
            return

        try:
            # The reactive reader may have rotated while this task waited.
            # Re-read inside the shared owned REST lock before consuming a token.
            if rest:
                db.refresh(record)
                if record.status not in ("active", "error"):
                    return
                current = decrypt_credentials(record.encrypted_credentials)
                if (current.get("account_id"), current.get("client_id")) != (account_id, client_id):
                    return
                if time.time() < current.get("expires_at", 0) - REFRESH_BUFFER_SECONDS:
                    return
                creds = current
                refresh_token = creds.get("refresh_token")
                if not refresh_token:
                    return
            token_data = _run_async_refresh(account_id, refresh_token, client_id)
            print(
                f"[proactive_token_refresh] token_data keys: {list(token_data.keys()) if isinstance(token_data, dict) else type(token_data)}",
                flush=True,
            )
            creds["access_token"] = token_data["access_token"]
            creds["refresh_token"] = token_data.get("refresh_token", refresh_token)
            creds["issued_at"] = time.time()
            creds["expires_in"] = int(token_data.get("expires_in", 3600))
            creds["expires_at"] = creds["issued_at"] + creds["expires_in"]
            record.encrypted_credentials = encrypt_credentials(creds)
            record.status = "active"
            record.error_reason = None
            record.last_health_check_at = now
            # Commit immediately after each refresh — if the worker is killed
            # before the final commit, the new tokens are already persisted.
            # NetSuite refresh tokens are single-use; losing them = dead connection.
            db.commit()
            stats["refreshed"] += 1
            logger.info(
                "proactive_token_refresh.refreshed",
                extra={"record_id": str(record.id), "prefix": lock_prefix},
            )
        except Exception as exc:
            stats["errors"] += 1
            logger.warning(
                "proactive_token_refresh.refresh_failed",
                extra={"record_id": str(record.id), "error": str(exc), "error_type": type(exc).__name__},
            )
            print(f"[proactive_token_refresh] REFRESH ERROR: {type(exc).__name__}: {exc}", flush=True)
        finally:
            if rest:
                oauth_refresh_lock.release(lock_key, owner)
            else:
                release_lock(lock_key)

    except Exception:
        stats["errors"] += 1
        logger.exception(
            "proactive_token_refresh.check_error",
            extra={"record_id": str(record.id)},
        )


def _run_async_refresh(account_id: str, refresh_token: str, client_id: str) -> dict:
    """Run the async refresh in a temporary event loop."""
    from app.services.netsuite_oauth_service import refresh_tokens_with_client

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(refresh_tokens_with_client(account_id, refresh_token, client_id))
    finally:
        loop.close()
