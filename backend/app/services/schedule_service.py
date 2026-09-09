import uuid
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import structlog
from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pipeline import Schedule

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Due computation (Scheduled Jobs platform, Slice 2, spec §B4) — shared by the
# Beat sweep (`app.workers.tasks.scheduled_jobs.run_due_jobs`, which decides
# WHICH schedules are due) and, in a later task, the approve/schedule-edit API
# endpoints (spec §B5), which need to seed/recompute a schedule's `next_run_at`
# the SAME way the sweep will later read it. One implementation, two callers —
# a schedule approved with a `next_run_at` computed by a different formula than
# the sweep's own due predicate would silently drift out of sync with the page
# that shows "next run" and the sweep that actually fires it.
# ---------------------------------------------------------------------------


def compute_next_run(cron: str, tz: str, after: datetime) -> datetime:
    """The next fire time strictly after ``after``, computed IN the schedule's
    own IANA timezone and converted back to UTC — DST-safe by construction
    (croniter walks wall-clock time in the zone you hand it): a weekly
    ``0 6 * * 1`` / ``America/Los_Angeles`` job is 13:00 UTC in September
    (PDT, UTC-7) and 14:00 UTC in January (PST, UTC-8), never a fixed UTC
    offset.

    ``after`` may be naive or aware; naive is treated as UTC (matches every
    other "now" in this codebase — ``datetime.now(timezone.utc)``)."""
    zone = ZoneInfo(tz)
    after_utc = after if after.tzinfo is not None else after.replace(tzinfo=timezone.utc)
    after_local = after_utc.astimezone(zone)
    next_local = croniter(cron, after_local).get_next(datetime)
    return next_local.astimezone(timezone.utc)


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
        select(Schedule).where(Schedule.tenant_id == tenant_id).order_by(Schedule.created_at.desc())
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
