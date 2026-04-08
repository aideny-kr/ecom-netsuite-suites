"""Builds a ConnectorState object for the disclosure hook.

Queries the tenant's connectors and connection_alerts table to determine
which sources are available and healthy. Conservative on errors: any query
failure treats the source as unavailable so we never show a bogus "switch
to X" hint when X is actually broken.

The returned object implements the ``ConnectorState`` Protocol defined in
``app.services.chat.disclosure`` (duck-typed; we don't inherit).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection
from app.models.connection_alert import ConnectionAlert
from app.models.mcp_connector import McpConnector


@dataclass
class _LiveConnectorState:
    has_bigquery: bool
    has_netsuite: bool
    bq_healthy: bool
    ns_healthy: bool
    bq_sync_age: timedelta


async def build_connector_state(db: AsyncSession, tenant_id: UUID) -> _LiveConnectorState:
    """Query the DB to assemble the connector state snapshot.

    Conservative defaults: any query failure → treat source as unavailable.

    Field-name substitutions vs. spec (documented for future maintainers):

    * ``ConnectionAlert`` has no ``severity`` or ``resolved_at`` columns —
      the model tracks dismissals via ``dismissed_at``. We therefore treat
      **any non-dismissed alert as a health signal**.
    * ``ConnectionAlert.connection_type`` uses ``rest_api``/``mcp`` values,
      not ``netsuite``/``bigquery``. We map ``rest_api`` → NetSuite (the
      REST API connector is the NetSuite data path) and inspect the
      associated ``McpConnector.provider`` to map MCP alerts to the right
      source.
    * ``McpConnector`` has no ``last_sync_at`` column. We fall back to
      ``last_health_check_at`` as the nearest age signal. If neither exists
      the sync age stays at the "very stale" sentinel and ``can_switch``
      becomes False by the conservative default.
    * ``Connection``/``McpConnector`` are considered present+healthy when
      ``status == "active"``.
    """
    # ── NetSuite REST connection ──
    ns_connection: Connection | None
    try:
        ns_result = await db.execute(
            select(Connection).where(
                Connection.tenant_id == tenant_id,
                Connection.provider == "netsuite",
            )
        )
        ns_connection = ns_result.scalars().first()
    except Exception:
        ns_connection = None

    # ── BigQuery MCP connector ──
    bq_connector: McpConnector | None
    try:
        bq_result = await db.execute(
            select(McpConnector).where(
                McpConnector.tenant_id == tenant_id,
                McpConnector.provider == "bigquery",
            )
        )
        bq_connector = bq_result.scalars().first()
    except Exception:
        bq_connector = None

    # ── Active connection alerts ──
    # NOTE: ConnectionAlert has no `severity` or `resolved_at`. We treat any
    # alert that has not been dismissed as a health signal. The model stores
    # `connection_type` as "rest_api" | "mcp"; we map to source names below.
    mcp_alert_connection_ids: set[UUID] = set()
    has_rest_api_alert = False
    try:
        alerts_result = await db.execute(
            select(ConnectionAlert).where(
                ConnectionAlert.tenant_id == tenant_id,
                ConnectionAlert.dismissed_at.is_(None),
            )
        )
        for alert in alerts_result.scalars().all():
            if alert.connection_type == "rest_api":
                has_rest_api_alert = True
            elif alert.connection_type == "mcp":
                mcp_alert_connection_ids.add(alert.connection_id)
    except Exception:
        # Conservative: missing alerts table means no alerts, not unhealthy.
        pass

    # ── Health: status=="active" AND no active alerts ──
    ns_healthy = (
        ns_connection is not None
        and getattr(ns_connection, "status", None) == "active"
        and not has_rest_api_alert
    )
    bq_healthy = (
        bq_connector is not None
        and getattr(bq_connector, "status", None) == "active"
        and bq_connector.id not in mcp_alert_connection_ids
    )

    # ── BigQuery sync age ──
    # Substitution: McpConnector has no last_sync_at; use last_health_check_at
    # as the nearest age signal. Default to a very-stale sentinel when missing
    # so compute_can_switch_source() treats BQ as stale and returns False.
    bq_sync_age = timedelta(days=999)
    if bq_connector is not None:
        last_seen = getattr(bq_connector, "last_health_check_at", None)
        if last_seen is not None:
            # Ensure we compare timezone-aware datetimes.
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            bq_sync_age = datetime.now(timezone.utc) - last_seen

    return _LiveConnectorState(
        has_bigquery=bq_connector is not None,
        has_netsuite=ns_connection is not None,
        bq_healthy=bq_healthy,
        ns_healthy=ns_healthy,
        bq_sync_age=bq_sync_age,
    )
