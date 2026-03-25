"""BigQuery tool executors for the MCP tool registry.

Three tools:
- bigquery_sql_execute — run read-only SQL
- bigquery_schema_execute — discover datasets/tables/columns
- bigquery_cost_estimate_execute — dry-run cost estimate
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.core.encryption import decrypt_credentials
from app.models.mcp_connector import McpConnector
from app.services.bigquery_service import (
    discover_schema,
    estimate_query_cost,
    execute_query,
)

logger = logging.getLogger(__name__)


async def _get_bigquery_connector(context: dict) -> McpConnector | None:
    """Look up the active BigQuery connector for the tenant."""
    db = context.get("db")
    tenant_id = context.get("tenant_id")
    if not db or not tenant_id:
        return None

    result = await db.execute(
        select(McpConnector).where(
            McpConnector.tenant_id == tenant_id,
            McpConnector.provider == "bigquery",
            McpConnector.status == "active",
        )
    )
    return result.scalars().first()


def _extract_credentials(connector: McpConnector) -> tuple[dict, str, str | None]:
    """Decrypt credentials and extract service account JSON, project_id, and location."""
    creds = decrypt_credentials(connector.encrypted_credentials)
    sa_json = creds.get("service_account_json", {})
    project_id = creds.get("project_id") or (connector.metadata_json or {}).get("project_id", "")
    location = creds.get("location") or (connector.metadata_json or {}).get("location")
    return sa_json, project_id, location


async def bigquery_sql_execute(params: dict, context: dict, **kwargs: Any) -> dict:
    """Execute a read-only BigQuery SQL query."""
    if not context or not context.get("tenant_id") or not context.get("db"):
        return {"error": True, "message": "Missing context — tenant_id and db are required."}

    connector = await _get_bigquery_connector(context)
    if not connector:
        return {"error": True, "message": "No active BigQuery connector found for this tenant."}

    sa_json, project_id, location = _extract_credentials(connector)
    query = params.get("query", "")
    max_rows = params.get("max_rows", 1000)

    try:
        result = await execute_query(
            credentials=sa_json,
            project_id=project_id,
            query=query,
            max_rows=max_rows,
            location=location,
        )
        return result
    except Exception as exc:
        logger.warning("BigQuery SQL execution failed", exc_info=True)
        return {"error": True, "message": f"BigQuery query failed: {exc}"}


async def bigquery_schema_execute(params: dict, context: dict, **kwargs: Any) -> dict:
    """Discover BigQuery datasets, tables, and columns."""
    if not context or not context.get("tenant_id") or not context.get("db"):
        return {"error": True, "message": "Missing context — tenant_id and db are required."}

    connector = await _get_bigquery_connector(context)
    if not connector:
        return {"error": True, "message": "No active BigQuery connector found for this tenant."}

    sa_json, project_id, location = _extract_credentials(connector)
    dataset = params.get("dataset")

    try:
        result = await discover_schema(
            credentials=sa_json,
            project_id=project_id,
            dataset=dataset,
            location=location,
        )

        # Filter to only show selected tables if configured
        selected = (connector.metadata_json or {}).get("selected_tables") if connector.metadata_json else None
        if selected:
            filtered_datasets = []
            for ds in result.get("datasets", []):
                ds_tables = selected.get(ds["dataset_id"], [])
                if ds_tables:
                    filtered = {
                        "dataset_id": ds["dataset_id"],
                        "tables": [t for t in ds["tables"] if t["table_id"] in ds_tables],
                    }
                    filtered_datasets.append(filtered)
            result = {"datasets": filtered_datasets}

        return result
    except Exception as exc:
        logger.warning("BigQuery schema discovery failed", exc_info=True)
        return {"error": True, "message": f"BigQuery schema discovery failed: {exc}"}


async def bigquery_cost_estimate_execute(params: dict, context: dict, **kwargs: Any) -> dict:
    """Estimate the cost of a BigQuery query via dry run."""
    if not context or not context.get("tenant_id") or not context.get("db"):
        return {"error": True, "message": "Missing context — tenant_id and db are required."}

    connector = await _get_bigquery_connector(context)
    if not connector:
        return {"error": True, "message": "No active BigQuery connector found for this tenant."}

    sa_json, project_id, location = _extract_credentials(connector)
    query = params.get("query", "")

    try:
        result = await estimate_query_cost(
            credentials=sa_json,
            project_id=project_id,
            query=query,
            location=location,
        )
        return result
    except Exception as exc:
        logger.warning("BigQuery cost estimation failed", exc_info=True)
        return {"error": True, "message": f"BigQuery cost estimation failed: {exc}"}
