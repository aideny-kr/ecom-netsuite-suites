from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.models.user import User
from app.services.table_service import ALLOWED_TABLES, export_table_csv, query_table

router = APIRouter(prefix="/tables", tags=["tables"])


@router.get("/{table_name}")
async def get_table(
    table_name: str,
    user: Annotated[User, Depends(require_permission("tables.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    sort_by: str | None = None,
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    status: str | None = None,
    currency: str | None = None,
    source: str | None = None,
):
    if table_name not in ALLOWED_TABLES:
        raise HTTPException(status_code=404, detail=f"Unknown table: {table_name}")

    filters: dict[str, Any] = {}
    if status:
        filters["status"] = status
    if currency:
        filters["currency"] = currency
    if source:
        filters["source"] = source

    result = await query_table(
        db=db,
        table_name=table_name,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
        filters=filters,
    )

    # Serialize items
    items = []
    for item in result["items"]:
        row = {}
        for col in item.__table__.columns:
            val = getattr(item, col.name)
            if val is not None:
                row[col.name] = str(val) if not isinstance(val, (int, float, bool, dict, list)) else val
            else:
                row[col.name] = None
        items.append(row)

    return {
        "items": items,
        "total": result["total"],
        "page": result["page"],
        "page_size": result["page_size"],
        "pages": result["pages"],
    }


@router.get("/{table_name}/export/csv")
async def export_csv(
    table_name: str,
    user: Annotated[User, Depends(require_permission("exports.csv"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if table_name not in ALLOWED_TABLES:
        raise HTTPException(status_code=404, detail=f"Unknown table: {table_name}")

    csv_content = await export_table_csv(db, table_name)
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={table_name}.csv"},
    )
