"""Service layer for Saved SuiteQL Analytics skill."""

import csv
import io
import re
import uuid
from typing import Any, Callable, Coroutine

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.saved_query import SavedSuiteQLQuery


async def get_saved_query(
    db: AsyncSession, query_id: uuid.UUID, tenant_id: uuid.UUID
) -> SavedSuiteQLQuery | None:
    """Fetch a saved query scoped to the tenant."""
    result = await db.execute(
        select(SavedSuiteQLQuery).where(
            SavedSuiteQLQuery.id == query_id,
            SavedSuiteQLQuery.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


async def delete_saved_query(
    db: AsyncSession, query_id: uuid.UUID, tenant_id: uuid.UUID
) -> bool:
    """Delete a saved query scoped to the tenant. Returns True if deleted."""
    result = await db.execute(
        delete(SavedSuiteQLQuery).where(
            SavedSuiteQLQuery.id == query_id,
            SavedSuiteQLQuery.tenant_id == tenant_id,
        )
    )
    return result.rowcount > 0


def inject_fetch_limit(query_text: str, limit: int = 500) -> str:
    """Inject FETCH FIRST N ROWS ONLY into a SuiteQL query.

    If the query already has a FETCH clause, replace it.
    Otherwise, append before any trailing semicolons.
    """
    fetch_clause = f"FETCH FIRST {limit} ROWS ONLY"

    # Remove existing FETCH clause if present
    cleaned = re.sub(
        r"\bFETCH\s+FIRST\s+\d+\s+ROWS\s+ONLY\b",
        "",
        query_text,
        flags=re.IGNORECASE,
    )

    # Strip trailing semicolons and whitespace
    cleaned = cleaned.rstrip().rstrip(";").rstrip()

    return f"{cleaned} {fetch_clause}"


async def paginate_suiteql(
    *,
    execute_fn: Callable[..., Coroutine[Any, Any, dict]],
    access_token: str,
    account_id: str,
    query: str,
    chunk_size: int = 1000,
) -> dict:
    """Paginate a SuiteQL query in chunks using OFFSET/FETCH FIRST.

    Loops until a page returns fewer rows than chunk_size.
    Returns aggregated result dict with all rows.
    """
    all_rows: list[list] = []
    columns: list[str] = []
    offset = 0

    while True:
        paginated_query = (
            f"{query.rstrip().rstrip(';').rstrip()} "
            f"OFFSET {offset} FETCH FIRST {chunk_size} ROWS ONLY"
        )
        result = await execute_fn(
            access_token=access_token,
            account_id=account_id,
            query=paginated_query,
            limit=chunk_size,
        )

        if not columns and result.get("columns"):
            columns = result["columns"]

        rows = result.get("rows", [])
        all_rows.extend(rows)

        if len(rows) < chunk_size:
            break

        offset += len(rows)

    return {
        "columns": columns,
        "rows": all_rows,
        "row_count": len(all_rows),
        "truncated": False,
    }


def rows_to_csv(columns: list[str], rows: list[list]) -> str:
    """Convert column/row data to CSV string."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    writer.writerows(rows)
    return output.getvalue()
