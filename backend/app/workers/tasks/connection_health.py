"""Periodic Celery task: check OAuth token health for all NetSuite connections.

Runs every 15 minutes via beat schedule. For each active connection:
- Decrypts credentials and checks expires_at
- If expired, attempts token refresh
- On failure, marks status="error" with an error_reason
- On success or still valid, stamps last_health_check_at
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="tasks.connection_health", queue="sync")
def check_connection_health():
    """Check OAuth token health for all NetSuite connections across tenants."""
    from app.core.encryption import decrypt_credentials
    from app.models.connection import Connection
    from app.models.mcp_connector import McpConnector
    from app.workers.base_task import sync_engine

    now = datetime.now(timezone.utc)
    stats = {"checked": 0, "healthy": 0, "expired": 0, "refreshed": 0, "errors": 0}

    with Session(sync_engine) as db:
        # ── Check Connection (OAuth REST API) ──
        connections = (
            db.execute(
                select(Connection).where(
                    Connection.provider == "netsuite",
                    Connection.status != "revoked",
                )
            )
            .scalars()
            .all()
        )

        for conn in connections:
            stats["checked"] += 1
            try:
                creds = decrypt_credentials(conn.encrypted_credentials)
                if creds.get("auth_type") != "oauth2":
                    conn.last_health_check_at = now
                    stats["healthy"] += 1
                    continue

                expires_at = creds.get("expires_at", 0)
                if time.time() < (expires_at - 60):
                    # Token is still valid
                    conn.last_health_check_at = now
                    if conn.status == "error":
                        # Clear stale error if token is actually fine
                        conn.status = "active"
                        conn.error_reason = None
                    stats["healthy"] += 1
                    continue

                # Token expired — attempt refresh
                stats["expired"] += 1
                refresh_token = creds.get("refresh_token")
                account_id = creds.get("account_id")
                client_id = creds.get("client_id")

                if not refresh_token or not account_id or not client_id:
                    conn.status = "error"
                    conn.error_reason = "Missing refresh credentials — re-authorize your NetSuite connection"
                    conn.last_health_check_at = now
                    stats["errors"] += 1
                    continue

                try:
                    token_data = _run_async_refresh(account_id, refresh_token, client_id)
                    # Refresh succeeded — update stored credentials
                    from app.core.encryption import encrypt_credentials

                    creds["access_token"] = token_data["access_token"]
                    creds["refresh_token"] = token_data.get("refresh_token", refresh_token)
                    creds["expires_at"] = time.time() + token_data.get("expires_in", 3600)
                    conn.encrypted_credentials = encrypt_credentials(creds)
                    conn.status = "active"
                    conn.error_reason = None
                    conn.last_health_check_at = now
                    stats["refreshed"] += 1
                    logger.info(
                        "connection_health.refreshed",
                        extra={"connection_id": str(conn.id)},
                    )
                except Exception as exc:
                    conn.status = "error"
                    conn.error_reason = "OAuth token expired — re-authorize your NetSuite connection"
                    conn.last_health_check_at = now
                    stats["errors"] += 1
                    logger.warning(
                        "connection_health.refresh_failed",
                        extra={"connection_id": str(conn.id), "error": str(exc)},
                    )

            except Exception:
                conn.last_health_check_at = now
                stats["errors"] += 1
                logger.exception(
                    "connection_health.check_error",
                    extra={"connection_id": str(conn.id)},
                )

        # ── Check McpConnector (OAuth MCP) ──
        mcp_connectors = (
            db.execute(
                select(McpConnector).where(
                    McpConnector.provider == "netsuite_mcp",
                    McpConnector.auth_type == "oauth2",
                    McpConnector.status != "revoked",
                )
            )
            .scalars()
            .all()
        )

        for mcp in mcp_connectors:
            stats["checked"] += 1
            try:
                if not mcp.encrypted_credentials:
                    mcp.last_health_check_at = now
                    stats["healthy"] += 1
                    continue

                creds = decrypt_credentials(mcp.encrypted_credentials)
                expires_at = creds.get("expires_at", 0)

                if time.time() < (expires_at - 60):
                    mcp.last_health_check_at = now
                    if mcp.status == "error":
                        mcp.status = "active"
                        mcp.error_reason = None
                    stats["healthy"] += 1
                    continue

                stats["expired"] += 1
                refresh_token = creds.get("refresh_token")
                account_id = creds.get("account_id")
                client_id = creds.get("client_id")

                if not refresh_token or not account_id or not client_id:
                    mcp.status = "error"
                    mcp.error_reason = "Missing refresh credentials — re-authorize your NetSuite MCP connection"
                    mcp.last_health_check_at = now
                    stats["errors"] += 1
                    continue

                try:
                    token_data = _run_async_refresh(account_id, refresh_token, client_id)
                    from app.core.encryption import encrypt_credentials

                    creds["access_token"] = token_data["access_token"]
                    creds["refresh_token"] = token_data.get("refresh_token", refresh_token)
                    creds["expires_at"] = time.time() + token_data.get("expires_in", 3600)
                    mcp.encrypted_credentials = encrypt_credentials(creds)
                    mcp.status = "active"
                    mcp.error_reason = None
                    mcp.last_health_check_at = now
                    stats["refreshed"] += 1
                    logger.info(
                        "connection_health.mcp_refreshed",
                        extra={"connector_id": str(mcp.id)},
                    )
                except Exception as exc:
                    mcp.status = "error"
                    mcp.error_reason = "OAuth token expired — re-authorize your NetSuite MCP connection"
                    mcp.last_health_check_at = now
                    stats["errors"] += 1
                    logger.warning(
                        "connection_health.mcp_refresh_failed",
                        extra={"connector_id": str(mcp.id), "error": str(exc)},
                    )

            except Exception:
                mcp.last_health_check_at = now
                stats["errors"] += 1
                logger.exception(
                    "connection_health.mcp_check_error",
                    extra={"connector_id": str(mcp.id)},
                )

        db.commit()

    logger.info("connection_health.completed", extra=stats)
    return stats


def _run_async_refresh(account_id: str, refresh_token: str, client_id: str) -> dict:
    """Run the async refresh_tokens_with_client in a temporary event loop."""
    from app.services.netsuite_oauth_service import refresh_tokens_with_client

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            refresh_tokens_with_client(account_id, refresh_token, client_id)
        )
    finally:
        loop.close()
