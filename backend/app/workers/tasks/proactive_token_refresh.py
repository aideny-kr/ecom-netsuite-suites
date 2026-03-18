"""Proactive token refresh — runs every 5 minutes.

Refreshes tokens BEFORE they expire, preventing the reactive-only pattern
where tokens go stale during idle periods. Uses Redis distributed lock
to prevent concurrent refresh race conditions.
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
    from app.core.encryption import decrypt_credentials, encrypt_credentials
    from app.core.redis_lock import acquire_lock, release_lock
    from app.models.connection import Connection
    from app.models.mcp_connector import McpConnector
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

        db.commit()

    logger.info("proactive_token_refresh.completed", extra=stats)
    print(f"[proactive_token_refresh] {stats}", flush=True)
    return stats


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
        if not acquire_lock(lock_key, timeout=30):
            stats["skipped_locked"] += 1
            return

        try:
            token_data = _run_async_refresh(account_id, refresh_token, client_id)
            print(f"[proactive_token_refresh] token_data keys: {list(token_data.keys()) if isinstance(token_data, dict) else type(token_data)}", flush=True)
            creds["access_token"] = token_data["access_token"]
            creds["refresh_token"] = token_data.get("refresh_token", refresh_token)
            creds["expires_at"] = time.time() + token_data.get("expires_in", 3600)
            record.encrypted_credentials = encrypt_credentials(creds)
            record.status = "active"
            record.error_reason = None
            record.last_health_check_at = now
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
