import csv
import io
from typing import Any

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import (
    Dispute,
    NetsuitePosting,
    Order,
    Payment,
    Payout,
    PayoutLine,
    Refund,
)

TABLE_MODEL_MAP = {
    "orders": Order,
    "payments": Payment,
    "refunds": Refund,
    "payouts": Payout,
    "payout_lines": PayoutLine,
    "disputes": Dispute,
    "netsuite_postings": NetsuitePosting,
}

ALLOWED_TABLES = set(TABLE_MODEL_MAP.keys())


def get_model_for_table(table_name: str):
    if table_name not in TABLE_MODEL_MAP:
        raise ValueError(f"Unknown table: {table_name}. Allowed: {ALLOWED_TABLES}")
    return TABLE_MODEL_MAP[table_name]


async def query_table(
    db: AsyncSession,
    table_name: str,
    page: int = 1,
    page_size: int = 50,
    sort_by: str | None = None,
    sort_order: str = "desc",
    filters: dict[str, Any] | None = None,
) -> dict:
    """Generic paginated query for canonical tables."""
    model = get_model_for_table(table_name)

    # Base query
    query = select(model)
    count_query = select(func.count()).select_from(model)

    # Apply filters
    if filters:
        for key, value in filters.items():
            if hasattr(model, key) and value is not None:
                column = getattr(model, key)
                query = query.where(column == value)
                count_query = count_query.where(column == value)

    # Apply sorting
    if sort_by and hasattr(model, sort_by):
        column = getattr(model, sort_by)
        query = query.order_by(column.desc() if sort_order == "desc" else column.asc())
    else:
        query = query.order_by(model.created_at.desc())

    # Count total
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Paginate
    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size)

    result = await db.execute(query)
    items = result.scalars().all()

    pages = (total + page_size - 1) // page_size if page_size > 0 else 0

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


async def export_table_csv(
    db: AsyncSession,
    table_name: str,
    filters: dict[str, Any] | None = None,
) -> str:
    """Export a canonical table to CSV string."""
    model = get_model_for_table(table_name)
    query = select(model)

    if filters:
        for key, value in filters.items():
            if hasattr(model, key) and value is not None:
                query = query.where(getattr(model, key) == value)

    query = query.limit(10000)  # Safety limit
    result = await db.execute(query)
    items = result.scalars().all()

    if not items:
        columns = [c.name for c in model.__table__.columns if c.name not in ("raw_data",)]
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(columns)
        return output.getvalue()

    columns = [c.name for c in model.__table__.columns if c.name not in ("raw_data",)]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    for item in items:
        writer.writerow([getattr(item, col, "") for col in columns])
    return output.getvalue()
