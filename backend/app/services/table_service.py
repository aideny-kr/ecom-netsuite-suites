import csv
import io
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
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
MAX_EXPORT_ROWS = 10000

_SEARCH_FIELDS = {
    "orders": ("order_number", "source_id"),
    "payments": ("source_id", "payment_method"),
    "refunds": ("source_id", "reason"),
    "payouts": ("source_id",),
    "payout_lines": ("source_id", "description", "related_order_id"),
    "disputes": ("source_id", "related_order_id", "reason"),
    "netsuite_postings": ("netsuite_internal_id", "record_type", "account_name", "memo"),
}


def _predicates(model, tenant_id: UUID, filters, search):
    # Explicit isolation is required even when a DB owner/bypass role runs the API.
    predicates = [model.tenant_id == tenant_id]
    for key, value in (filters or {}).items():
        if key in model.__table__.columns and value is not None:
            predicates.append(getattr(model, key) == value)
    if search and search.strip():
        literal = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        predicates.append(
            or_(
                *(
                    getattr(model, field).ilike(f"%{literal}%", escape="\\")
                    for field in _SEARCH_FIELDS[model.__tablename__]
                )
            )
        )
    return predicates


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
    *,
    tenant_id: UUID,
    search: str | None = None,
) -> dict:
    """Generic paginated query for canonical tables."""
    model = get_model_for_table(table_name)

    predicates = _predicates(model, tenant_id, filters, search)
    query = select(model).where(*predicates)
    count_query = select(func.count()).select_from(model).where(*predicates)
    if sort_by and (sort_by not in model.__table__.columns or sort_by == "raw_data"):
        raise ValueError("Invalid sort column")
    column = getattr(model, sort_by or "created_at")
    query = query.order_by(column.desc() if sort_order == "desc" else column.asc(), model.id.asc())

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
    *,
    tenant_id: UUID,
    search: str | None = None,
) -> str:
    """Export a canonical table to CSV string."""
    model = get_model_for_table(table_name)
    columns = [c for c in model.__table__.columns if c.name != "raw_data"]
    query = (
        select(*columns)
        .where(*_predicates(model, tenant_id, filters, search))
        .order_by(model.created_at.desc(), model.id.asc())
    )

    # Fetch one extra row so an oversized financial export fails explicitly.
    query = query.limit(MAX_EXPORT_ROWS + 1)
    result = await db.execute(query)
    items = result.all()
    if len(items) > MAX_EXPORT_ROWS:
        raise ValueError(f"Export exceeds {MAX_EXPORT_ROWS:,} rows. Please narrow your filters.")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([c.name for c in columns])
    for item in items:
        writer.writerow(item)
    return output.getvalue()
