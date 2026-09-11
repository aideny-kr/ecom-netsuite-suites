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


class BigQueryClientError(RuntimeError):
    """Delta gate round 3, item 4: raised by ``_get_client`` when the
    service-account credentials cannot construct a working BigQuery client
    (malformed/expired JSON, bad project). A dedicated type — rather than
    the bare ``ValueError`` this used to raise — so a caller can distinguish
    "the client/credentials themselves are broken" (an infra problem) from a
    plan-authoring ``ValueError`` such as ``_validate_read_only``'s
    rejection (a genuine plan defect). See
    ``app.services.jobs.compiler._bigquery_preflight``, which classifies a
    ``BigQueryClientError`` as ``PreflightUnavailable``, never a plan
    defect."""


def _get_client(credentials: dict, project_id: str, location: str | None = None) -> bigquery.Client:
    """Create a BigQuery client from service account JSON."""
    try:
        creds = service_account.Credentials.from_service_account_info(credentials)
        return bigquery.Client(credentials=creds, project=project_id, location=location or "US")
    except Exception as e:
        raise BigQueryClientError(f"Failed to initialize BigQuery client: {e}") from e


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


def _sync_dry_run_job(credentials: dict, project_id: str, query: str, location: str | None = None):
    """Submit ``query`` to BigQuery as a dry run (no bytes billed, no
    execution) and return the resulting job. A dry run round-trips through
    BigQuery's OWN parser/planner, so an invalid query raises BigQuery's real
    error (an unqualified table name, an unknown column, a syntax error) —
    without a regex ever having to reimplement BigQuery's SQL grammar.

    The ONE place that builds a dry-run client + job config — both
    ``estimate_query_cost`` (below, needs ``total_bytes_processed``) and
    ``dry_run_query`` (needs only "did this raise") call this, so there is
    exactly one BigQuery client construction path for a dry run, never two."""
    client = _get_client(credentials, project_id, location=location)
    job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    return client.query(query, job_config=job_config)


async def dry_run_query(
    credentials: dict,
    project_id: str,
    query: str,
    location: str | None = None,
) -> None:
    """Validate ``query`` against BigQuery without running it or billing any
    bytes (brief H, item 1 — the compiler's compile-time preflight over a
    ``bigquery_sql`` step, replacing the deleted regex heuristic in
    ``app.services.jobs.registry`` that had both false positives
    — e.g. ``EXTRACT(DAY FROM created_at)`` or ``FROM UNNEST (...)`` with a
    space — and false negatives — e.g. ``FROM a, b`` never checking ``b``).
    Raises on an invalid query (BigQuery's own error); returns ``None`` on a
    valid one — a caller only ever cares whether this raised.

    Delta gate (brief I, item 2): calls ``_validate_read_only`` first, exactly
    like ``execute_query``/``estimate_query_cost`` already do — without this,
    an ``INSERT``/``UPDATE``/``DELETE`` step passed the compiler's
    compile-time preflight cleanly and only ever failed at RUN time."""
    _validate_read_only(query)
    await asyncio.to_thread(_sync_dry_run_job, credentials, project_id, query, location)


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

    job = await asyncio.to_thread(_sync_dry_run_job, credentials, project_id, query, location)
    estimated_bytes = job.total_bytes_processed
    estimated_cost = estimated_bytes / 1_000_000_000_000 * 5

    return {
        "estimated_bytes": estimated_bytes,
        "estimated_cost_usd": estimated_cost,
    }
