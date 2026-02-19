"""API endpoint for viewing NetSuite API exchange logs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.netsuite_api_log import NetSuiteApiLog
from app.models.user import User

router = APIRouter(prefix="/netsuite", tags=["netsuite-api-logs"])


@router.get("/api-logs")
async def list_api_logs(
    limit: int = Query(default=50, le=200),
    source: str | None = None,
    status: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List recent NetSuite API exchange logs for the tenant."""
    q = select(NetSuiteApiLog).where(
        NetSuiteApiLog.tenant_id == user.tenant_id,
    )
    if source:
        q = q.where(NetSuiteApiLog.source == source)
    if status == "error":
        q = q.where((NetSuiteApiLog.response_status >= 400) | (NetSuiteApiLog.error_message.isnot(None)))
    q = q.order_by(NetSuiteApiLog.created_at.desc()).limit(limit)

    result = await db.execute(q)
    logs = result.scalars().all()

    return [
        {
            "id": str(log.id),
            "direction": log.direction,
            "method": log.method,
            "url": log.url,
            "response_status": log.response_status,
            "response_time_ms": log.response_time_ms,
            "error_message": log.error_message,
            "source": log.source,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
        for log in logs
    ]
