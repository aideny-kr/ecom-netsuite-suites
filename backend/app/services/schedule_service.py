import uuid
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pipeline import Schedule

logger = structlog.get_logger()


async def create_schedule(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    name: str,
    schedule_type: str,
    cron_expression: Optional[str] = None,
    parameters: Optional[dict] = None,
) -> Schedule:
    """Create a new schedule for a tenant."""
    schedule = Schedule(
        tenant_id=tenant_id,
        name=name,
        schedule_type=schedule_type,
        cron_expression=cron_expression,
        is_active=True,
        parameters=parameters,
    )
    db.add(schedule)
    await db.flush()
    return schedule


async def list_schedules(db: AsyncSession, tenant_id: uuid.UUID) -> list[Schedule]:
    """List all schedules for a tenant."""
    result = await db.execute(
        select(Schedule)
        .where(Schedule.tenant_id == tenant_id)
        .order_by(Schedule.created_at.desc())
    )
    return list(result.scalars().all())


async def delete_schedule(db: AsyncSession, schedule_id: uuid.UUID, tenant_id: uuid.UUID) -> bool:
    """Delete a schedule owned by the given tenant. Returns True if deleted, False if not found."""
    result = await db.execute(
        select(Schedule).where(
            Schedule.id == schedule_id,
            Schedule.tenant_id == tenant_id,
        )
    )
    schedule = result.scalar_one_or_none()
    if not schedule:
        return False
    await db.delete(schedule)
    await db.flush()
    return True
