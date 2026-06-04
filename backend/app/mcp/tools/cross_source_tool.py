"""cross_source_query tool — deterministic cross-source join.

Re-runs two source queries (SuiteQL / BigQuery) to materialize full bounded
rows (tenant-filtered by the existing source paths), joins them in-process via
the DuckDB-backed engine, and returns one {columns, rows} table through the
data_table interception path. The LLM never does the join math.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Any
from uuid import UUID

from app.mcp.tools.pivot_tool import _strip_row_limit
from app.services.join_service import join_rows

logger = logging.getLogger(__name__)

_MAX_ROWS_PER_SIDE = 10000


async def _run_source(query: str, dialect: str, context: dict) -> dict:
    """Fetch one source's full (bounded) rows, tenant-filtered. Mirrors pivot_tool."""
    from sqlalchemy import select

    from app.core.encryption import decrypt_credentials

    db = context.get("db")
    tenant_id_str = context.get("tenant_id")
    if not db or not tenant_id_str:
        raise ValueError("Database session and tenant_id required")
    tenant_id = UUID(tenant_id_str) if isinstance(tenant_id_str, str) else tenant_id_str

    d = "bigquery" if dialect == "bigquery" else "suiteql"
    clean = _strip_row_limit(query, dialect=d)

    if d == "bigquery":
        from app.models.mcp_connector import McpConnector
        from app.services.bigquery_service import execute_query

        res = await db.execute(
            select(McpConnector).where(
                McpConnector.tenant_id == tenant_id,
                McpConnector.provider == "bigquery",
                McpConnector.status == "active",
            )
        )
        connector = res.scalars().first()
        if not connector:
            raise ValueError("No active BigQuery connector for this tenant")
        creds = decrypt_credentials(connector.encrypted_credentials)
        sa_json = creds.get("service_account_json", {})
        project_id = creds.get("project_id") or (connector.metadata_json or {}).get("project_id", "")
        location = creds.get("location") or (connector.metadata_json or {}).get("location")
        raw = await execute_query(
            credentials=sa_json,
            project_id=project_id,
            query=clean,
            max_rows=_MAX_ROWS_PER_SIDE,
            location=location,
        )
    else:
        from app.models.connection import Connection
        from app.services.netsuite_client import execute_suiteql_via_rest
        from app.services.netsuite_oauth_service import get_valid_token

        res = await db.execute(
            select(Connection)
            .where(
                Connection.tenant_id == tenant_id,
                Connection.provider == "netsuite",
                Connection.status == "active",
            )
            .order_by(Connection.updated_at.desc())
            .limit(1)
        )
        connection = res.scalar_one_or_none()
        if not connection:
            raise ValueError("No active NetSuite connection for this tenant")
        access_token = await get_valid_token(db, connection)
        if not access_token:
            raise ValueError("NetSuite OAuth token expired — re-authorize in Settings")
        creds = decrypt_credentials(connection.encrypted_credentials)
        account_id = creds.get("account_id", "")
        raw = await execute_suiteql_via_rest(
            access_token=access_token,
            account_id=account_id,
            query=clean,
            limit=_MAX_ROWS_PER_SIDE,
        )

    rows = raw.get("rows", []) or []
    return {
        "columns": raw.get("columns", []),
        "rows": rows,
        "truncated": bool(raw.get("truncated", False)) or len(rows) >= _MAX_ROWS_PER_SIDE,
    }


async def execute(params: dict, context: dict | None = None, **kwargs: Any) -> dict:
    """Run both source queries, join them deterministically, return one table."""
    ctx = context or {}
    if not ctx.get("db") or not ctx.get("tenant_id"):
        return {"error": "Database session and tenant_id required"}

    left_query = params.get("left_query", "")
    right_query = params.get("right_query", "")
    left_dialect = params.get("left_dialect", "suiteql")
    right_dialect = params.get("right_dialect", "suiteql")
    join_keys = params.get("join_keys") or []
    join_type = params.get("join_type", "inner")
    select = params.get("select")

    if not left_query or not right_query:
        return {"error": "left_query and right_query are required"}
    if not join_keys:
        return {"error": 'join_keys required, e.g. [{"left": "sku", "right": "item"}]'}

    try:
        left = await _run_source(left_query, left_dialect, ctx)
    except Exception as e:  # noqa: BLE001 — surface a structured error, never crash the turn
        return {"error": f"Left source ({left_dialect}) failed: {str(e)[:300]}"}
    try:
        right = await _run_source(right_query, right_dialect, ctx)
    except Exception as e:  # noqa: BLE001
        return {"error": f"Right source ({right_dialect}) failed: {str(e)[:300]}"}

    tmpdir = os.path.join(tempfile.gettempdir(), "duckdb")
    os.makedirs(tmpdir, exist_ok=True)
    try:
        result = await asyncio.to_thread(
            join_rows, left, right, join_keys, join_type, select, ("_l", "_r"), "256MB", tmpdir
        )
    except ValueError as e:
        return {"error": str(e)}

    warnings: list[str] = []
    if left.get("truncated"):
        warnings.append(f"Left source truncated at {_MAX_ROWS_PER_SIDE} rows — join is partial.")
    if right.get("truncated"):
        warnings.append(f"Right source truncated at {_MAX_ROWS_PER_SIDE} rows — join is partial.")
    if result["row_count"] == 0:
        warnings.append("No rows matched the join key(s) — check the join key columns.")

    result.update(
        {
            "left_row_count": len(left["rows"]),
            "right_row_count": len(right["rows"]),
            "left_truncated": left.get("truncated", False),
            "right_truncated": right.get("truncated", False),
            "warnings": warnings,
        }
    )
    return result
