"""Connection alert endpoints — list and dismiss OAuth failure notifications."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_permission
from app.models.connection_alert import ConnectionAlert
from app.models.user import User

router = APIRouter(prefix="/connection-alerts", tags=["connection-alerts"])


class ConnectionAlertResponse(BaseModel):
    id: str
    connection_type: str
    connection_id: str
    alert_type: str
    message: str
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("", response_model=list[ConnectionAlertResponse])
async def list_active_alerts(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List active (undismissed) connection alerts for the tenant."""
    result = await db.execute(
        select(ConnectionAlert)
        .where(
            ConnectionAlert.tenant_id == user.tenant_id,
            ConnectionAlert.dismissed_at.is_(None),
        )
        .order_by(ConnectionAlert.created_at.desc())
        .limit(20)
    )
    alerts = result.scalars().all()
    return [
        ConnectionAlertResponse(
            id=str(a.id),
            connection_type=a.connection_type,
            connection_id=str(a.connection_id),
            alert_type=a.alert_type,
            message=a.message,
            created_at=a.created_at,
        )
        for a in alerts
    ]


@router.post("/{alert_id}/dismiss", status_code=status.HTTP_200_OK)
async def dismiss_alert(
    alert_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Dismiss a connection alert (admin only)."""
    result = await db.execute(
        select(ConnectionAlert).where(
            ConnectionAlert.id == alert_id,
            ConnectionAlert.tenant_id == user.tenant_id,
        )
    )
    alert = result.scalar_one_or_none()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    alert.dismissed_by = user.id
    alert.dismissed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"status": "dismissed"}
