"""Periodic Celery task: audit OAuth token health for all NetSuite connections.

Runs every 15 minutes via beat schedule. Acts as a STATUS AUDIT — the proactive
token refresh task (every 5 min) handles actual refresh. This task:
- Checks token validity and stamps last_health_check_at
- If token is expired and proactive refresh hasn't fixed it, sets status="error"
- Clears stale errors if token is actually valid
"""

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="tasks.connection_health", queue="sync")
def check_connection_health():
    """Audit OAuth token health for all NetSuite connections across tenants."""
    from app.core.encryption import decrypt_credentials
    from app.models.connection import Connection
    from app.models.mcp_connector import McpConnector
    from app.workers.base_task import sync_engine

    now = datetime.now(timezone.utc)
    stats = {"checked": 0, "healthy": 0, "expired": 0, "errors": 0}

    with Session(sync_engine) as db:
        # ── Audit Connection (OAuth REST API) ──
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
                if not conn.encrypted_credentials:
                    conn.last_health_check_at = now
                    stats["healthy"] += 1
                    continue

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
                        conn.status = "active"
                        conn.error_reason = None
                    stats["healthy"] += 1
                else:
                    # Token expired — proactive refresh should have handled this.
                    # If we're here, refresh failed. Mark as error.
                    stats["expired"] += 1
                    if conn.status != "error":
                        conn.status = "error"
                        conn.error_reason = "OAuth token expired — re-authorize your NetSuite connection"
                        logger.warning(
                            "connection_health.token_expired",
                            extra={"connection_id": str(conn.id)},
                        )
                    conn.last_health_check_at = now

            except Exception:
                conn.last_health_check_at = now
                stats["errors"] += 1
                logger.exception(
                    "connection_health.check_error",
                    extra={"connection_id": str(conn.id)},
                )

        # ── Audit McpConnector (OAuth MCP) ──
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
                else:
                    stats["expired"] += 1
                    if mcp.status != "error":
                        mcp.status = "error"
                        mcp.error_reason = "OAuth token expired — re-authorize your NetSuite MCP connection"
                        logger.warning(
                            "connection_health.mcp_token_expired",
                            extra={"connector_id": str(mcp.id)},
                        )
                    mcp.last_health_check_at = now

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
