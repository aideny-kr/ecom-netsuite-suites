import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.models.user import User
from app.schemas.schedule import ScheduleCreate, ScheduleResponse
from app.services import audit_service, entitlement_service, schedule_service

router = APIRouter(prefix="/schedules", tags=["schedules"])


@router.get("", response_model=list[ScheduleResponse])
async def list_schedules(
    user: Annotated[User, Depends(require_permission("schedules.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
):
    """List all schedules for the current tenant."""
    schedules = await schedule_service.list_schedules(db, user.tenant_id)
    return [
        ScheduleResponse(
            id=str(s.id),
            tenant_id=str(s.tenant_id),
            name=s.name,
            schedule_type=s.schedule_type,
            cron_expression=s.cron_expression,
            is_active=s.is_active,
            parameters=s.parameters,
        )
        for s in schedules
    ]


@router.post("", response_model=ScheduleResponse, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    body: ScheduleCreate,
    user: Annotated[User, Depends(require_permission("schedules.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
):
    """Create a new schedule for the current tenant, subject to plan quota."""
    # Quota check
    allowed = await entitlement_service.check_entitlement(db, user.tenant_id, "schedules")
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Schedule limit reached for your plan",
        )

    schedule = await schedule_service.create_schedule(
        db=db,
        tenant_id=user.tenant_id,
        name=body.name,
        schedule_type=body.schedule_type,
        cron_expression=body.cron_expression,
        parameters=body.parameters,
    )

    correlation_id = request.headers.get("X-Correlation-ID")
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="schedule",
        action="schedule.create",
        actor_id=user.id,
        resource_type="schedule",
        resource_id=str(schedule.id),
        correlation_id=correlation_id,
        payload={"name": body.name, "schedule_type": body.schedule_type},
    )
    await db.commit()
    await db.refresh(schedule)

    return ScheduleResponse(
        id=str(schedule.id),
        tenant_id=str(schedule.tenant_id),
        name=schedule.name,
        schedule_type=schedule.schedule_type,
        cron_expression=schedule.cron_expression,
        is_active=schedule.is_active,
        parameters=schedule.parameters,
    )


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    schedule_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("schedules.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
):
    """Delete a schedule owned by the current tenant."""
    deleted = await schedule_service.delete_schedule(db, schedule_id, user.tenant_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")

    correlation_id = request.headers.get("X-Correlation-ID")
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="schedule",
        action="schedule.delete",
        actor_id=user.id,
        resource_type="schedule",
        resource_id=str(schedule_id),
        correlation_id=correlation_id,
    )
    await db.commit()
