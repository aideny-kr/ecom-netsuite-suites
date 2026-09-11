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
    """Reject DML/DDL. Raises ValueError.

    NOTE (round 4, fix/jobs-live-run-defects): this is a CHEAP first line of
    defense only -- it checks the leading keyword of a comment-stripped copy
    of the query, nothing more. A previous round added a `;`-split here to
    also reject multi-statement scripts, but that was a text heuristic on
    SQL and it failed both directions: it REJECTED a legitimate single
    statement with a semicolon inside a string literal
    (`SELECT * FROM t WHERE region = 'us;east'`), and it FAILED TO REJECT
    `SELECT '--'; DELETE FROM d.t` -- `_strip_sql_comments` doesn't
    understand string literals either, so it saw the `--` inside the quotes
    as a real comment and cleaned the query down to `SELECT '`, a single
    "statement" that still starts with SELECT, while the ORIGINAL
    two-statement text is what actually reaches `execute_query`/
    `dry_run_query`. The `;`-split is removed; multi-statement/script
    rejection now lives in `dry_run_query`'s `statement_type` check, which
    asks BigQuery's OWN parser to classify the ORIGINAL query text -- a
    string literal or comment cannot fool BigQuery's parser the way it
    fools a regex.
    """
    cleaned = _strip_sql_comments(query).strip()
    # Allow SELECT and WITH (CTEs)
    if not (cleaned.upper().startswith("SELECT") or cleaned.upper().startswith("WITH")):
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

    NOTE (round 4): `_validate_read_only`'s leading-keyword check is a cheap
    first line of defense only -- it never asks BigQuery's own parser
    anything, so it cannot see a multi-statement script or DML/DDL hidden
    past a leading SELECT the way `dry_run_query`'s `statement_type` check
    can (see that function's docstring). Follow-up, not done here: add an
    equivalent dry-run `statement_type` gate in front of execution for this
    BI-tool surface -- do NOT add a dry run to `execute_query` casually,
    since every call here already pays for one real query execution.
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
    compile-time preflight cleanly and only ever failed at RUN time.

    Round 4 (fix/jobs-live-run-defects): also reads the dry-run job's OWN
    ``statement_type`` (``google-cloud-bigquery``'s ``QueryJob.statement_type``
    -- a multi-statement script reports ``"SCRIPT"``, DML reports e.g.
    ``"DELETE"``/``"INSERT"``, a plain query reports ``"SELECT"``) and raises
    ``ValueError`` for anything other than ``"SELECT"``. This replaces the
    ``;``-split multi-statement guard removed from ``_validate_read_only``:
    a dry run round-trips through BigQuery's OWN parser against the ORIGINAL
    query text (the exact text ``execute_query`` would send), so neither a
    semicolon inside a string literal nor a comment-like sequence inside one
    can fool it the way they fooled a regex."""
    _validate_read_only(query)
    job = await asyncio.to_thread(_sync_dry_run_job, credentials, project_id, query, location)
    statement_type = getattr(job, "statement_type", None)
    if statement_type != "SELECT":
        raise ValueError(f"bigquery preflight: only SELECT statements are allowed (got {statement_type})")


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
