"""BigQuery service — query execution, schema discovery, cost estimation.

All BigQuery client calls are synchronous (google-cloud-bigquery SDK).
Async wrappers use asyncio.to_thread() to avoid blocking the event loop.
Tests mock _get_client so the sync calls are instant.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from google.cloud import bigquery
from google.oauth2 import service_account


def _get_client(credentials: dict, project_id: str, location: str | None = None) -> bigquery.Client:
    """Create a BigQuery client from service account JSON."""
    try:
        creds = service_account.Credentials.from_service_account_info(credentials)
        return bigquery.Client(credentials=creds, project=project_id, location=location or "US")
    except Exception as e:
        raise ValueError(f"Failed to initialize BigQuery client: {e}")


def _strip_sql_comments(query: str) -> str:
    """Remove SQL comments from a query string.

    Strips:
    - Block comments: /* ... */ (non-greedy, handles multi-line)
    - Single-line comments: -- ... to end of line
    - Leading/trailing whitespace after stripping

    NOTE: Does not handle comment-like syntax inside string literals.
    Safe for _validate_read_only (only checks first keyword), but do NOT
    use this to transform queries before execution.
    """
    # Remove block comments first (non-greedy, DOTALL for multi-line)
    cleaned = re.sub(r"/\*.*?\*/", "", query, flags=re.DOTALL)
    # Remove single-line comments
    cleaned = re.sub(r"--[^\n]*", "", cleaned)
    return cleaned.strip()


def _validate_read_only(query: str) -> None:
    """Reject DML/DDL. Raises ValueError for non-SELECT queries."""
    cleaned = _strip_sql_comments(query).strip().upper()
    # Allow SELECT and WITH (CTEs)
    if cleaned.startswith("SELECT") or cleaned.startswith("WITH"):
        return
    raise ValueError("Read-only queries only — SELECT and WITH/CTE are allowed")


async def execute_query(
    credentials: dict,
    project_id: str,
    query: str,
    max_rows: int = 1000,
    max_bytes_billed: int = 1_000_000_000,
    timeout_seconds: int = 30,
    location: str | None = None,
) -> dict[str, Any]:
    """Execute a read-only BigQuery SQL query.

    Returns {"columns", "rows", "row_count", "bytes_processed", "truncated", "cache_hit", "query"}.
    """
    _validate_read_only(query)

    def _sync_execute():
        client = _get_client(credentials, project_id, location=location)
        job_config = bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes_billed)
        job = client.query(query, job_config=job_config)
        result = job.result(timeout=timeout_seconds)

        columns = [field.name for field in result.schema]
        rows: list[list[Any]] = []
        truncated = False
        for row in result:
            if len(rows) >= max_rows:
                truncated = True
                break
            rows.append(row.values())

        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "bytes_processed": job.total_bytes_processed,
            "truncated": truncated,
            "cache_hit": job.cache_hit,
            "query": query,
        }

    return await asyncio.to_thread(_sync_execute)


async def discover_schema(
    credentials: dict,
    project_id: str,
    dataset: str | None = None,
    location: str | None = None,
) -> dict[str, Any]:
    """Discover BigQuery datasets and tables.

    If dataset is provided, returns columns for tables in that dataset.
    Otherwise, lists all datasets with their tables (no column detail).
    """

    def _sync_discover():
        client = _get_client(credentials, project_id, location=location)

        if dataset:
            # Single dataset — include column details
            tables_list = list(client.list_tables(dataset))
            tables = []
            for tbl in tables_list:
                full_table = client.get_table(tbl)
                columns = [
                    {
                        "name": field.name,
                        "type": field.field_type,
                        "description": getattr(field, "description", None),
                    }
                    for field in full_table.schema
                ]
                tables.append({"table_id": tbl.table_id, "columns": columns})
            return {"datasets": [{"dataset_id": dataset, "tables": tables}]}

        # All datasets
        datasets = []
        for ds in client.list_datasets():
            tables_list = list(client.list_tables(ds.dataset_id))
            tables = [{"table_id": t.table_id} for t in tables_list]
            datasets.append({"dataset_id": ds.dataset_id, "tables": tables})
        return {"datasets": datasets}

    return await asyncio.to_thread(_sync_discover)


async def validate_connection(
    credentials: dict,
    project_id: str,
    location: str | None = None,
) -> dict[str, Any]:
    """Validate BigQuery connectivity by running SELECT 1."""

    def _sync_validate():
        client = _get_client(credentials, project_id, location=location)
        job = client.query("SELECT 1")
        job.result()

    try:
        await asyncio.to_thread(_sync_validate)
        return {"valid": True, "error": None}
    except Exception as exc:
        return {"valid": False, "error": str(exc)}


async def estimate_query_cost(
    credentials: dict,
    project_id: str,
    query: str,
    location: str | None = None,
) -> dict[str, Any]:
    """Dry-run a query to estimate cost.

    Pricing: $5 per TB = bytes / 1_000_000_000_000 * 5
    """
    _validate_read_only(query)

    def _sync_estimate():
        client = _get_client(credentials, project_id, location=location)
        job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        job = client.query(query, job_config=job_config)
        return job.total_bytes_processed

    estimated_bytes = await asyncio.to_thread(_sync_estimate)
    estimated_cost = estimated_bytes / 1_000_000_000_000 * 5

    return {
        "estimated_bytes": estimated_bytes,
        "estimated_cost_usd": estimated_cost,
    }
