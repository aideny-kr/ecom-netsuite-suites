from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.models.audit import AuditEvent
from app.models.user import User
from app.schemas.common import AuditEventResponse, PaginatedResponse

router = APIRouter(prefix="/audit-events", tags=["audit"])


@router.get("", response_model=PaginatedResponse[AuditEventResponse])
async def list_audit_events(
    user: Annotated[User, Depends(require_permission("audit.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    category: str | None = None,
    action: str | None = None,
    correlation_id: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
):
    query = select(AuditEvent).where(AuditEvent.tenant_id == user.tenant_id)
    count_query = select(func.count()).select_from(AuditEvent).where(AuditEvent.tenant_id == user.tenant_id)

    if category:
        query = query.where(AuditEvent.category == category)
        count_query = count_query.where(AuditEvent.category == category)
    if action:
        query = query.where(AuditEvent.action == action)
        count_query = count_query.where(AuditEvent.action == action)
    if correlation_id:
        query = query.where(AuditEvent.correlation_id == correlation_id)
        count_query = count_query.where(AuditEvent.correlation_id == correlation_id)
    if date_from:
        query = query.where(AuditEvent.timestamp >= date_from)
        count_query = count_query.where(AuditEvent.timestamp >= date_from)
    if date_to:
        query = query.where(AuditEvent.timestamp <= date_to)
        count_query = count_query.where(AuditEvent.timestamp <= date_to)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    offset = (page - 1) * page_size
    query = query.order_by(AuditEvent.timestamp.desc()).offset(offset).limit(page_size)

    result = await db.execute(query)
    events = result.scalars().all()

    items = [
        AuditEventResponse(
            id=e.id,
            tenant_id=str(e.tenant_id),
            timestamp=e.timestamp.isoformat() if e.timestamp else "",
            actor_id=str(e.actor_id) if e.actor_id else None,
            actor_type=e.actor_type,
            category=e.category,
            action=e.action,
            resource_type=e.resource_type,
            resource_id=e.resource_id,
            correlation_id=e.correlation_id,
            job_id=str(e.job_id) if e.job_id else None,
            payload=e.payload,
            status=e.status,
            error_message=e.error_message,
        )
        for e in events
    ]

    pages = (total + page_size - 1) // page_size if page_size > 0 else 0
    return PaginatedResponse(items=items, total=total, page=page, page_size=page_size, pages=pages)


@router.get("/retention-stats")
async def get_audit_retention_stats(
    user: Annotated[User, Depends(require_permission("audit.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get audit retention statistics for the current tenant."""
    from app.services.audit_retention import get_retention_stats
    stats = await get_retention_stats(db, tenant_id=user.tenant_id)
    return stats
