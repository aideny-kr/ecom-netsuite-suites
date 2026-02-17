import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.table_service import TABLE_MODEL_MAP

logger = logging.getLogger(__name__)

ALLOWED_TABLES = frozenset(TABLE_MODEL_MAP.keys())

MAX_ROWS = 20


async def execute(params: dict, **kwargs) -> dict:
    """Read sample data from an allowlisted canonical table.

    Uses the DB session and tenant_id from the MCP governance context
    when available; falls back to column-only metadata otherwise.
    """
    table_name = params.get("table_name", "")

    if table_name not in ALLOWED_TABLES:
        raise ValueError(f"Table '{table_name}' is not in the allowlist. Allowed tables: {sorted(ALLOWED_TABLES)}")

    context: dict = kwargs.get("context", {})
    db: AsyncSession | None = context.get("db")
    _tenant_id = context.get("tenant_id")

    model = TABLE_MODEL_MAP[table_name]

    # Without a DB session, return schema-only info
    if db is None:
        columns = [c.name for c in model.__table__.columns]
        return {
            "table": table_name,
            "columns": columns,
            "rows": [],
            "row_count": 0,
        }

    try:
        query = select(model).order_by(model.created_at.desc()).limit(MAX_ROWS)
        result = await db.execute(query)
        rows_raw = result.scalars().all()

        rows = [{k: str(v) for k, v in row.__dict__.items() if not k.startswith("_")} for row in rows_raw]
        columns = list(rows[0].keys()) if rows else [c.name for c in model.__table__.columns]

        return {
            "table": table_name,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
        }
    except Exception:
        logger.warning("data.sample_table_read failed for %s, returning empty", table_name, exc_info=True)
        columns = [c.name for c in model.__table__.columns]
        return {
            "table": table_name,
            "columns": columns,
            "rows": [],
            "row_count": 0,
        }
