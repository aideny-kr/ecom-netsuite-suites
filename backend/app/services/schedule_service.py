import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

import structlog
from croniter import croniter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job
from app.models.pipeline import Schedule
from app.models.user import User
from app.services import audit_service, entitlement_service
from app.services.jobs.compiler import Clarification, compile_instruction

if TYPE_CHECKING:
    from app.schemas.schedule import ScheduleCreate

logger = structlog.get_logger()


class QuotaExceeded(Exception):  # noqa: N818 — interface name from item 5's gate-fix brief, not a generic Error
    """Raised by ``create_scheduled_job`` when the tenant's plan-quota
    entitlement check (``entitlement_service.check_entitlement(...,
    "schedules")``) fails — the same check ``POST /schedules`` has always
    made, now shared with the MCP ``schedule.create`` tool's instruction
    branch (item 5, gate fix) so the two create paths cannot drift out of
    sync on it."""


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
    owner_id: Optional[uuid.UUID] = None,
    created_via: Optional[str] = None,
) -> Schedule:
    """Create a new schedule for a tenant. `owner_id`/`created_via` (Task 5
    residual, spec §B6) are optional so this legacy direct-create path stays
    callable exactly as it always was for any caller that doesn't have them."""
    schedule = Schedule(
        tenant_id=tenant_id,
        name=name,
        schedule_type=schedule_type,
        cron_expression=cron_expression,
        is_active=True,
        parameters=parameters,
        owner_id=owner_id,
        created_via=created_via,
    )
    db.add(schedule)
    await db.flush()
    return schedule


async def create_scheduled_job(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    body: "ScheduleCreate",
    actor_id: Optional[uuid.UUID],
    created_via: str,
) -> Schedule:
    """Item 5 (gate fix): the ONE instruction-create path for both
    `POST /schedules` and the MCP `schedule.create` tool's instruction
    branch — before this fix, the MCP handler built its `Schedule` row by
    hand, bypassing BOTH the plan-quota entitlement check the API route
    always made AND every `ScheduleCreate` validator (instruction/name
    length, cron_expression/timezone from item 1) since it never constructed
    that model at all. Callers on both sides now build a `ScheduleCreate`
    from their own input shape (validators run there, at construction) and
    hand it to this one function for the entitlement check + compile +
    persist.

    Raises `QuotaExceeded` when the tenant's plan quota is exhausted, or
    `Clarification` (also an `Exception` — see its own docstring) when the
    compiler needs more detail instead of a plan. Only flushes — never
    commits, per this codebase's "service flushes, endpoint commits once"
    convention (`.claude/rules/sqlalchemy-fastapi.md`); the caller (the API
    route, or the MCP tool handler) owns the commit.
    """
    allowed = await entitlement_service.check_entitlement(db, tenant_id, "schedules")
    if not allowed:
        raise QuotaExceeded("Schedule limit reached for your plan")

    compiled = await compile_instruction(
        db,
        tenant_id=tenant_id,
        instruction=body.instruction,
        actor_id=actor_id,
        plan_version=0,
    )
    if isinstance(compiled, Clarification):
        raise compiled

    schedule = Schedule(
        tenant_id=tenant_id,
        name=body.name or default_job_name(body.instruction),
        schedule_type="job",
        cron_expression=body.cron_expression,
        timezone=body.timezone or "UTC",
        is_active=True,
        instruction=body.instruction,
        plan_json=compiled.plan_json,
        plan_version=0,
        plan_status="pending_approval",
        delivery_json=body.delivery,
        owner_id=actor_id,
        created_via=created_via,
    )
    db.add(schedule)
    await db.flush()

    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="schedule",
        action="schedule.create",
        actor_id=actor_id,
        resource_type="schedule",
        resource_id=str(schedule.id),
        payload={"instruction": body.instruction, "plan_status": "pending_approval", "model": compiled.model},
    )
    return schedule


async def list_schedules(db: AsyncSession, tenant_id: uuid.UUID) -> list[Schedule]:
    """List all schedules for a tenant."""
    result = await db.execute(
        select(Schedule).where(Schedule.tenant_id == tenant_id).order_by(Schedule.created_at.desc())
    )
    return list(result.scalars().all())


async def get_schedule(db: AsyncSession, schedule_id: uuid.UUID, tenant_id: uuid.UUID) -> Optional[Schedule]:
    """Fetch one schedule scoped to a tenant, or ``None`` — the shared lookup
    every Slice 2 (spec §B5) endpoint uses so "not found" and "belongs to a
    different tenant" both read as a 404, never a 403 that would leak
    existence across tenants."""
    result = await db.execute(select(Schedule).where(Schedule.id == schedule_id, Schedule.tenant_id == tenant_id))
    return result.scalar_one_or_none()


def default_job_name(instruction: str) -> str:
    """A schedule name derived from its instruction when the caller (API or
    MCP, spec §B5: "`{name?, instruction, ...}`") omits one — the first line,
    truncated to a title-sized length, never the whole instruction verbatim."""
    first_line = instruction.strip().splitlines()[0] if instruction.strip() else "Scheduled job"
    return first_line[:77] + "..." if len(first_line) > 80 else first_line


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


# ---------------------------------------------------------------------------
# List-page aggregates (Task 5 residual, spec §B6) — one or two queries
# total, never one per schedule row (N+1). `Job.parameters["schedule_id"]`
# is the same JSON lookup `GET /schedules/{id}/runs` already uses
# (`app/api/v1/schedules.py`'s own `list_runs`); comparing it as text
# (`.astext`), not casting to UUID, matches that established convention.
# ---------------------------------------------------------------------------


async def schedule_run_stats(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    schedule_ids: list[uuid.UUID],
    *,
    now: Optional[datetime] = None,
) -> dict[uuid.UUID, dict]:
    """Per-schedule `{"last_run_duration_seconds": float | None,
    "runs_last_7_days": int}` for every id in `schedule_ids` — TWO queries
    total regardless of how many schedules there are, never one per row:

    - a Postgres `DISTINCT ON` pick of each schedule's most recent `jobs`
      row (by `started_at`), to compute the duration of ITS last run;
    - a `GROUP BY` count of each schedule's `jobs` rows in the trailing 7
      days.

    A schedule absent from either result (never run, or no runs in the last
    7 days) is simply missing from the returned dict — callers read via
    `.get(schedule_id)` / `.get(schedule_id, {})`, never index directly.
    """
    if not schedule_ids:
        return {}
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    schedule_id_expr = Job.parameters["schedule_id"].astext
    schedule_id_strs = [str(sid) for sid in schedule_ids]

    duration_stmt = (
        select(schedule_id_expr.label("schedule_id"), Job.started_at, Job.completed_at)
        .where(
            Job.tenant_id == tenant_id,
            Job.job_type == "scheduled_job",
            schedule_id_expr.in_(schedule_id_strs),
            Job.completed_at.isnot(None),
        )
        .distinct(schedule_id_expr)
        .order_by(schedule_id_expr, Job.started_at.desc())
    )
    duration_rows = (await db.execute(duration_stmt)).all()

    count_stmt = (
        select(schedule_id_expr.label("schedule_id"), func.count().label("cnt"))
        .where(
            Job.tenant_id == tenant_id,
            Job.job_type == "scheduled_job",
            schedule_id_expr.in_(schedule_id_strs),
            Job.started_at >= cutoff,
        )
        .group_by(schedule_id_expr)
    )
    count_rows = (await db.execute(count_stmt)).all()

    stats: dict[uuid.UUID, dict] = {}
    for row in duration_rows:
        try:
            sid = uuid.UUID(row.schedule_id)
        except (TypeError, ValueError):
            continue
        duration = None
        if row.started_at is not None and row.completed_at is not None:
            duration = (row.completed_at - row.started_at).total_seconds()
        stats.setdefault(sid, {})["last_run_duration_seconds"] = duration
    for row in count_rows:
        try:
            sid = uuid.UUID(row.schedule_id)
        except (TypeError, ValueError):
            continue
        stats.setdefault(sid, {})["runs_last_7_days"] = row.cnt
    return stats


async def tenant_run_totals_7d(
    db: AsyncSession, tenant_id: uuid.UUID, *, now: Optional[datetime] = None
) -> tuple[int, int]:
    """Tenant-wide `(total, failed)` run counts in the trailing 7 days — ONE
    aggregate query for the list page's "Last 7 days" tile (spec §B6), never
    per-schedule. `failed` matches `Job.status == "failed"` (set by
    `_finalize_run` for `reason in (error, stall)`, `app.workers.tasks.
    scheduled_jobs`) — the SAME vocabulary every other reader of that column
    already uses, not a re-derivation from `result_summary`."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    stmt = select(
        func.count().label("total"),
        func.count().filter(Job.status == "failed").label("failed"),
    ).where(Job.tenant_id == tenant_id, Job.job_type == "scheduled_job", Job.started_at >= cutoff)
    row = (await db.execute(stmt)).one()
    return int(row.total or 0), int(row.failed or 0)


async def owner_names(db: AsyncSession, owner_ids: list[Optional[uuid.UUID]]) -> dict[uuid.UUID, str]:
    """`{owner_id: full_name}` for every id in `owner_ids` — ONE query, not
    one per schedule row (spec §B6's Job column sub-line, "owner {name}")."""
    ids = [oid for oid in owner_ids if oid is not None]
    if not ids:
        return {}
    result = await db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
    return {row.id: row.full_name for row in result.all()}
