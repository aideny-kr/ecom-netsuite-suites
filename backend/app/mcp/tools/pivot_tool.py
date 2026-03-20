"""Pivot query result tool — server-side deterministic pivoting.

Re-executes a SuiteQL query without row limits and pivots the result
in Python. Only values that exist in the data become columns.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.pivot_service import pivot_rows

logger = logging.getLogger(__name__)

_FETCH_FIRST_RE = re.compile(r"\s*FETCH\s+FIRST\s+\d+\s+ROWS\s+ONLY\s*$", re.IGNORECASE)
_ROWNUM_RE = re.compile(r"\s+AND\s+ROWNUM\s*<=\s*\d+", re.IGNORECASE)


def _strip_row_limit(query: str) -> str:
    """Remove FETCH FIRST N ROWS ONLY and ROWNUM limits from query."""
    query = _FETCH_FIRST_RE.sub("", query)
    query = _ROWNUM_RE.sub("", query)
    return query.strip()


async def execute(
    *,
    query: str,
    row_field: str,
    column_field: str,
    value_field: str,
    aggregation: str = "sum",
    include_total: bool = True,
    tenant_id: UUID | None = None,
    actor_id: UUID | None = None,
    correlation_id: str | None = None,
    db: AsyncSession | None = None,
    **kwargs: Any,
) -> str:
    """Execute a SuiteQL query and pivot the result.

    1. Strip row limits from query
    2. Re-execute via REST API (up to 10,000 rows)
    3. Pivot using pivot_rows()
    4. Return pivoted table as JSON
    """
    from app.models.connection import Connection
    from app.services.netsuite_client import execute_suiteql_via_rest
    from app.services.netsuite_oauth_service import get_valid_token
    from sqlalchemy import select

    if not db or not tenant_id:
        return json.dumps({"error": "Database session and tenant_id required"})

    # Get active connection
    result = await db.execute(
        select(Connection).where(
            Connection.tenant_id == tenant_id,
            Connection.provider == "netsuite",
            Connection.status == "active",
        )
        .order_by(Connection.updated_at.desc())
        .limit(1)
    )
    connection = result.scalar_one_or_none()
    if not connection:
        return json.dumps({"error": "No active NetSuite connection"})

    # Get valid token
    access_token = await get_valid_token(db, connection)
    if not access_token:
        return json.dumps({"error": "OAuth token expired — re-authorize in Settings"})

    from app.core.encryption import decrypt_credentials
    creds = decrypt_credentials(connection.encrypted_credentials)
    account_id = creds.get("account_id", "")

    # Strip row limit and re-execute
    clean_query = _strip_row_limit(query)
    logger.info("pivot_tool.executing", extra={"query_len": len(clean_query)})

    try:
        raw_result = await execute_suiteql_via_rest(
            access_token=access_token,
            account_id=account_id,
            query=clean_query,
            limit=10000,
        )
    except Exception as e:
        return json.dumps({"error": f"Query execution failed: {str(e)[:300]}"})

    # Parse result
    columns = raw_result.get("columns", [])
    rows = raw_result.get("rows", [])

    if not rows:
        return json.dumps({"columns": [row_field], "rows": [], "row_count": 0, "pivoted": True})

    # Pivot
    try:
        out_columns, out_rows = pivot_rows(
            columns=columns,
            rows=rows,
            row_field=row_field,
            column_field=column_field,
            value_field=value_field,
            aggregation=aggregation,
            include_total=include_total,
        )
    except ValueError as e:
        return json.dumps({"error": str(e)})

    return json.dumps({
        "columns": out_columns,
        "rows": out_rows,
        "row_count": len(out_rows),
        "pivoted": True,
        "pivot_config": {
            "row_field": row_field,
            "column_field": column_field,
            "value_field": value_field,
            "aggregation": aggregation,
        },
    }, default=str)
