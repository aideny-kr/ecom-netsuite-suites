"""Read-only, explicitly projected live transaction evidence for authorized operators."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_feature, require_permission
from app.models.user import User
from app.services.transaction_ops.source_reader import (
    SourceReadError,
    read_framework_order,
    read_framework_orders_page,
)

router = APIRouter(
    prefix="/transaction-sources", tags=["transaction-sources"], dependencies=[Depends(require_feature("celigo"))]
)


class SourceEvidenceOut(BaseModel):
    source: Literal["framework"]
    celigo_step_id: str
    connection_id: str
    read_at: datetime
    scope: Literal["order", "updated_orders"]
    # source_projection.py names every business field explicitly before data
    # reaches this response. Never return a source object or ORM dump here.
    orders: list[dict[str, JsonValue]]
    page_complete: bool
    window_complete: bool
    next_page: int | None
    updated_since: datetime | None = None
    page: int | None = None
    page_size: int | None = None
    total_count: int | None = None
    pages: int | None = None


@router.get("/celigo/steps/{step_id}/orders/{order_reference}", response_model=SourceEvidenceOut)
async def get_framework_order(
    step_id: uuid.UUID,
    order_reference: Annotated[str, Path(pattern=r"^R[0-9]{9}(?:-[A-Z0-9]+)?$", max_length=100)],
    user: Annotated[User, Depends(require_permission("connections.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        return await read_framework_order(db, user.tenant_id, step_id, order_reference)
    except SourceReadError as exc:
        raise HTTPException(status_code=exc.http_status, detail={"code": exc.code}) from None


@router.get("/celigo/steps/{step_id}/orders", response_model=SourceEvidenceOut)
async def get_framework_orders_page(
    step_id: uuid.UUID,
    updated_since: datetime,
    user: Annotated[User, Depends(require_permission("connections.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: Annotated[int, Query(ge=1, le=100000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=20)] = 20,
):
    try:
        return await read_framework_orders_page(db, user.tenant_id, step_id, updated_since, page, page_size)
    except SourceReadError as exc:
        raise HTTPException(status_code=exc.http_status, detail={"code": exc.code}) from None
