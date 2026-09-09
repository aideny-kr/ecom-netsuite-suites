"""Scheduled Jobs platform — API (Slice 2, spec §B5, binding).

Two families of route live on the same `schedules` table (spec §B1): the
pre-Slice-2 opaque-parameter-bag schedules (`schedule_type` in
`sync|report|recon`, unchanged below) and the new Scheduled Job
(`schedule_type == "job"`), compiled from a plain-language `instruction` via
`app.services.jobs.compiler.compile_instruction` and replayed, unattended, by
`app.workers.tasks.scheduled_jobs.run_schedule_now` — never the other way
around; the agent runs ONLY at compile time (`create_schedule`/
`update_schedule` below), never inside `run_schedule`.

Permission: `schedules.manage` on every route (spec §B5); quota
(`entitlement_service.check_entitlement(..., "schedules")`) unchanged, gating
`POST /schedules` only, exactly as it did pre-Slice-2.
"""

import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.models.job import Job
from app.models.pipeline import Schedule
from app.models.user import User
from app.schemas.schedule import (
    DiffLineOut,
    ScheduleCreate,
    ScheduleDetailResponse,
    ScheduleResponse,
    ScheduleRunItem,
    ScheduleRunRequest,
    ScheduleRunResponse,
    ScheduleUpdate,
)
from app.services import audit_service, entitlement_service, schedule_service
from app.services.jobs.compiler import Clarification, compile_instruction, plan_diff
from app.services.jobs.registry import STEP_REGISTRY
from app.workers.tasks.scheduled_jobs import run_schedule_now

router = APIRouter(prefix="/schedules", tags=["schedules"])


# ---------------------------------------------------------------------------
# Response builders — one place that maps a `Schedule` ORM row to the wire
# shape, so every route below renders the Scheduled Job fields identically.
# ---------------------------------------------------------------------------


def _plan_kinds(plan_json: dict | None) -> list[str]:
    """READ/WRITE tags for the list/detail pages (spec §B6: "kind tags
    derived from the plan") — looked up in `STEP_REGISTRY` fresh, the same
    choke point the executor itself uses, so a step type retired from the
    registry after a plan was compiled simply stops contributing a tag
    rather than raising here."""
    if not plan_json:
        return []
    kinds: set[str] = set()
    for step in plan_json.get("steps") or []:
        spec = STEP_REGISTRY.get(step.get("type"))
        if spec is not None:
            kinds.add(spec.kind)
    return sorted(kinds)


def _plan_summary_line(plan_json: dict | None) -> str | None:
    """The table's one-line "Does" summary — mirrors
    `compiler._build_compiled_plan`'s rendering exactly (steps count + a
    de-duplicated arrow chain of step labels) without importing that
    compile-time-only helper into the API layer."""
    if not plan_json:
        return None
    steps = plan_json.get("steps") or []
    if not steps:
        return None
    labels: list[str] = []
    for step in steps:
        spec = STEP_REGISTRY.get(step.get("type"))
        label = spec.label if spec is not None else str(step.get("type"))
        if not labels or labels[-1] != label:
            labels.append(label)
    return f"{len(steps)} steps · " + " → ".join(labels)


def _to_response(schedule: Schedule) -> ScheduleResponse:
    return ScheduleResponse(
        id=str(schedule.id),
        tenant_id=str(schedule.tenant_id),
        name=schedule.name,
        schedule_type=schedule.schedule_type,
        cron_expression=schedule.cron_expression,
        is_active=schedule.is_active,
        parameters=schedule.parameters,
        instruction=schedule.instruction,
        plan_status=schedule.plan_status,
        plan_version=schedule.plan_version,
        timezone=schedule.timezone,
        delivery_json=schedule.delivery_json,
        budget_json=schedule.budget_json,
        catch_up=schedule.catch_up,
        last_run_at=schedule.last_run_at,
        last_run_status=schedule.last_run_status,
        next_run_at=schedule.next_run_at,
        paused_at=schedule.paused_at,
        pause_reason=schedule.pause_reason,
        kinds=_plan_kinds(schedule.plan_json),
        summary_line=_plan_summary_line(schedule.plan_json),
        has_pending_plan=schedule.pending_plan_json is not None,
    )


def _to_detail_response(schedule: Schedule) -> ScheduleDetailResponse:
    base = _to_response(schedule)
    diff: list[DiffLineOut] = []
    if schedule.pending_plan_json:
        diff = [DiffLineOut(**asdict(line)) for line in plan_diff(schedule.plan_json or {}, schedule.pending_plan_json)]
    return ScheduleDetailResponse(
        **base.model_dump(),
        plan_json=schedule.plan_json,
        pending_plan_json=schedule.pending_plan_json,
        pending_plan_reason=schedule.pending_plan_reason,
        pending_plan_diff=diff,
        owner_id=str(schedule.owner_id) if schedule.owner_id else None,
    )


async def _get_or_404(db: AsyncSession, schedule_id: uuid.UUID, tenant_id: uuid.UUID) -> Schedule:
    schedule = await schedule_service.get_schedule(db, schedule_id, tenant_id)
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    return schedule


def _correlation_id(request: Request) -> str | None:
    return request.headers.get("X-Correlation-ID")


# ---------------------------------------------------------------------------
# List / detail
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ScheduleResponse])
async def list_schedules(
    user: Annotated[User, Depends(require_permission("schedules.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
):
    """List all schedules for the current tenant (spec §B5/§B6: last run
    status/at, next_run_at, kind tags, delivery summary, and
    `has_pending_plan` — an approved schedule whose instruction was edited
    since, so it has a recompiled `pending_plan_json` awaiting approval; the
    list page's own gate is `schedules.manage`, not the detail-only view that
    would otherwise reveal this)."""
    schedules = await schedule_service.list_schedules(db, user.tenant_id)
    return [_to_response(s) for s in schedules]


@router.get("/{schedule_id}", response_model=ScheduleDetailResponse)
async def get_schedule(
    schedule_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("schedules.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Full detail: instruction, plan, pending plan + diff, schedule,
    delivery, budget (spec §B5)."""
    schedule = await _get_or_404(db, schedule_id, user.tenant_id)
    return _to_detail_response(schedule)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=ScheduleResponse, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    body: ScheduleCreate,
    user: Annotated[User, Depends(require_permission("schedules.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
):
    """Create a new schedule for the current tenant, subject to plan quota.

    Two shapes (see `ScheduleCreate`'s docstring): `instruction` given ->
    compiles into a Scheduled Job, returning `201` with `plan_status =
    "pending_approval"` and the compiled plan, or raising `409` with the
    compiler's clarification question and creating NOTHING (spec §B5) —
    mirrors `POST .../approve`'s "nothing persisted on the failure path"
    convention. `instruction` absent -> the legacy direct-create path,
    unchanged from pre-Slice-2 behaviour.
    """
    allowed = await entitlement_service.check_entitlement(db, user.tenant_id, "schedules")
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Schedule limit reached for your plan",
        )

    correlation_id = _correlation_id(request)

    if body.instruction:
        compiled = await compile_instruction(
            db,
            tenant_id=user.tenant_id,
            instruction=body.instruction,
            actor_id=user.id,
            plan_version=0,
        )
        if isinstance(compiled, Clarification):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"clarification": compiled.question},
            )

        schedule = Schedule(
            tenant_id=user.tenant_id,
            name=body.name or schedule_service.default_job_name(body.instruction),
            schedule_type="job",
            cron_expression=body.cron_expression,
            timezone=body.timezone or "UTC",
            is_active=True,
            instruction=body.instruction,
            plan_json=compiled.plan_json,
            plan_version=0,
            plan_status="pending_approval",
            delivery_json=body.delivery,
        )
        db.add(schedule)
        await db.flush()

        await audit_service.log_event(
            db=db,
            tenant_id=user.tenant_id,
            category="schedule",
            action="schedule.create",
            actor_id=user.id,
            resource_type="schedule",
            resource_id=str(schedule.id),
            correlation_id=correlation_id,
            payload={"instruction": body.instruction, "plan_status": "pending_approval", "model": compiled.model},
        )
        await db.commit()
        await db.refresh(schedule)
        return _to_response(schedule)

    schedule = await schedule_service.create_schedule(
        db=db,
        tenant_id=user.tenant_id,
        name=body.name,
        schedule_type=body.schedule_type,
        cron_expression=body.cron_expression,
        parameters=body.parameters,
    )

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
    return _to_response(schedule)


# ---------------------------------------------------------------------------
# Update (instruction recompile + direct field edits)
# ---------------------------------------------------------------------------


@router.patch("/{schedule_id}", response_model=ScheduleDetailResponse)
async def update_schedule(
    schedule_id: uuid.UUID,
    body: ScheduleUpdate,
    user: Annotated[User, Depends(require_permission("schedules.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
):
    """`instruction` -> recompile (spec §B5): a schedule that already has an
    APPROVED plan gets the recompiled plan in `pending_plan_json` (diff shown
    against the live `plan_json`, approval required before it runs); a
    schedule that has never been approved yet (still `pending_approval` from
    its own creation, or `draft`) just gets its one plan replaced directly —
    there is no live plan to diff against or protect. Every other field
    applies immediately, no approval needed.
    """
    schedule = await _get_or_404(db, schedule_id, user.tenant_id)
    correlation_id = _correlation_id(request)
    changed_fields: dict = {}

    if body.name is not None:
        schedule.name = body.name
        changed_fields["name"] = body.name
    if body.cron_expression is not None:
        schedule.cron_expression = body.cron_expression
        changed_fields["cron_expression"] = body.cron_expression
    if body.timezone is not None:
        schedule.timezone = body.timezone
        changed_fields["timezone"] = body.timezone
    if body.delivery is not None:
        schedule.delivery_json = body.delivery
        changed_fields["delivery_json"] = body.delivery
    if body.budget is not None:
        schedule.budget_json = body.budget
        changed_fields["budget_json"] = body.budget
    if body.catch_up is not None:
        schedule.catch_up = body.catch_up
        changed_fields["catch_up"] = body.catch_up
    if body.discard_pending:
        schedule.pending_plan_json = None
        schedule.pending_plan_reason = None
        changed_fields["discard_pending"] = True

    if body.instruction is not None:
        compiled = await compile_instruction(
            db,
            tenant_id=user.tenant_id,
            instruction=body.instruction,
            actor_id=user.id,
            plan_version=schedule.plan_version,
        )
        if isinstance(compiled, Clarification):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"clarification": compiled.question},
            )

        schedule.instruction = body.instruction
        if schedule.plan_status == "approved" and schedule.plan_json:
            schedule.pending_plan_json = compiled.plan_json
            schedule.pending_plan_reason = "instruction edited"
        else:
            schedule.plan_json = compiled.plan_json
            schedule.plan_status = "pending_approval"
        changed_fields["instruction"] = True

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="schedule",
        action="schedule.update",
        actor_id=user.id,
        resource_type="schedule",
        resource_id=str(schedule_id),
        correlation_id=correlation_id,
        payload={"changed": sorted(changed_fields)},
    )
    await db.commit()
    await db.refresh(schedule)
    return _to_detail_response(schedule)


# ---------------------------------------------------------------------------
# Approve / run / pause / resume
# ---------------------------------------------------------------------------


@router.post("/{schedule_id}/approve", response_model=ScheduleDetailResponse)
async def approve_schedule(
    schedule_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("schedules.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
):
    """Pending -> approved, `plan_version + 1` (spec §B5, verbatim), whether
    this is the schedule's first-ever approval (its ONLY plan is `plan_json`,
    still sitting at `plan_status == "pending_approval"` from creation) or a
    later pending-CHANGE approval (`pending_plan_json` promoted over
    `plan_json`)."""
    schedule = await _get_or_404(db, schedule_id, user.tenant_id)

    if schedule.pending_plan_json:
        schedule.plan_json = schedule.pending_plan_json
        schedule.pending_plan_json = None
        schedule.pending_plan_reason = None
    elif schedule.plan_status != "pending_approval" or not schedule.plan_json:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No pending plan to approve")

    schedule.plan_status = "approved"
    schedule.plan_version += 1

    if schedule.cron_expression:
        schedule.next_run_at = schedule_service.compute_next_run(
            schedule.cron_expression, schedule.timezone, datetime.now(timezone.utc)
        )

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="schedule",
        action="schedule.approve",
        actor_id=user.id,
        resource_type="schedule",
        resource_id=str(schedule_id),
        correlation_id=_correlation_id(request),
        payload={"plan_version": schedule.plan_version},
    )
    await db.commit()
    await db.refresh(schedule)
    return _to_detail_response(schedule)


@router.post("/{schedule_id}/run", response_model=ScheduleRunResponse)
async def run_schedule(
    schedule_id: uuid.UUID,
    body: ScheduleRunRequest,
    user: Annotated[User, Depends(require_permission("schedules.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Run one schedule now (spec §B5: "enqueues one run now; records the
    plan version used"). Calls `run_schedule_now` directly on this request's
    own session — per that function's own docstring it is written to be
    "directly callable ... from ... a request-scoped 'Run now' endpoint",
    not routed through a Celery dispatch, so the response can carry the real
    `jobs` row id it just created rather than only a fire-and-forget task id.
    `run_schedule_now` owns its own transaction boundaries (audit-before-write
    commits, the final commit) — this route makes no additional writes.
    """
    schedule = await _get_or_404(db, schedule_id, user.tenant_id)
    plan_to_run = schedule.pending_plan_json if body.use_pending else schedule.plan_json
    if not plan_to_run or not plan_to_run.get("steps"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No compiled plan to run" if not body.use_pending else "No pending plan to run",
        )
    # HITL gate (review finding): a non-empty `plan_json` is not the same as
    # a person having approved it — `use_pending=False` must not be able to
    # run a plan still sitting at `plan_status == "pending_approval"` (e.g.
    # straight off `POST /schedules`, before anyone has clicked Approve).
    # `use_pending=True` is exempt on purpose: "Run once with this change"
    # (spec §B5) previews an edited-but-not-yet-approved `pending_plan_json`
    # BEFORE approval, by design — `run_schedule_now` enforces the same rule
    # (see its docstring) so the MCP tool inherits it too.
    if not body.use_pending and schedule.plan_status != "approved":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Plan is not approved")

    outcome = await run_schedule_now(
        db,
        schedule_id,
        tenant_id=user.tenant_id,
        actor_id=user.id,
        actor_type="user",
        use_pending=body.use_pending,
    )
    return ScheduleRunResponse(
        jobs_id=str(outcome.jobs_row_id) if outcome.jobs_row_id else None,
        reason=outcome.reason,
        outputs=outcome.outputs,
    )


@router.post("/{schedule_id}/pause", response_model=ScheduleResponse)
async def pause_schedule(
    schedule_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("schedules.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
):
    """Manual pause — the same fields the executor's own retry-then-pause
    path (spec §B4) writes, so the page renders identically regardless of
    whether a person or the sweep paused it."""
    schedule = await _get_or_404(db, schedule_id, user.tenant_id)
    schedule.paused_at = datetime.now(timezone.utc)
    schedule.pause_reason = "Paused by operator"

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="schedule",
        action="schedule.pause",
        actor_id=user.id,
        resource_type="schedule",
        resource_id=str(schedule_id),
        correlation_id=_correlation_id(request),
    )
    await db.commit()
    await db.refresh(schedule)
    return _to_response(schedule)


@router.post("/{schedule_id}/resume", response_model=ScheduleResponse)
async def resume_schedule(
    schedule_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("schedules.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
):
    """Clear the pause and, for an approved schedule with a cron, recompute
    `next_run_at` from now — a schedule paused for days must not immediately
    look "due" for every missed tick the moment it resumes."""
    schedule = await _get_or_404(db, schedule_id, user.tenant_id)
    schedule.paused_at = None
    schedule.pause_reason = None
    if schedule.last_run_status == "paused":
        schedule.last_run_status = None

    if schedule.plan_status == "approved" and schedule.cron_expression:
        schedule.next_run_at = schedule_service.compute_next_run(
            schedule.cron_expression, schedule.timezone, datetime.now(timezone.utc)
        )

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="schedule",
        action="schedule.resume",
        actor_id=user.id,
        resource_type="schedule",
        resource_id=str(schedule_id),
        correlation_id=_correlation_id(request),
    )
    await db.commit()
    await db.refresh(schedule)
    return _to_response(schedule)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@router.get("/{schedule_id}/runs", response_model=list[ScheduleRunItem])
async def list_runs(
    schedule_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("schedules.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(50, ge=1, le=500),
):
    """Runs from `jobs` (spec §B5) — every `run_schedule_now` call writes one
    row there with `parameters["schedule_id"]` set to this schedule's id
    (`app.workers.tasks.scheduled_jobs.run_schedule_now`); tenant-scoped on
    `Job.tenant_id` too so this can never leak another tenant's row even if
    a schedule id were guessed."""
    await _get_or_404(db, schedule_id, user.tenant_id)  # 404s cleanly if not this tenant's

    result = await db.execute(
        select(Job)
        .where(
            Job.tenant_id == user.tenant_id,
            Job.parameters["schedule_id"].astext == str(schedule_id),
        )
        .order_by(Job.created_at.desc())
        .limit(limit)
    )
    jobs = result.scalars().all()
    items: list[ScheduleRunItem] = []
    for job in jobs:
        summary = job.result_summary or {}
        params = job.parameters or {}
        items.append(
            ScheduleRunItem(
                id=str(job.id),
                status=job.status,
                reason=summary.get("reason"),
                started_at=job.started_at,
                completed_at=job.completed_at,
                correlation_id=job.correlation_id,
                plan_version=params.get("plan_version"),
                attempt=params.get("attempt"),
                outputs=summary.get("outputs") or {},
                detail=summary.get("detail"),
            )
        )
    return items


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


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
