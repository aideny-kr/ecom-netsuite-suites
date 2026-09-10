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
    ScheduleListResponse,
    ScheduleResponse,
    ScheduleRunItem,
    ScheduleRunRequest,
    ScheduleRunResponse,
    ScheduleUpdate,
)
from app.services import audit_service, entitlement_service, schedule_service
from app.services.jobs.compiler import Clarification, compile_instruction, plan_diff
from app.services.jobs.registry import STEP_REGISTRY
from app.workers.celery_app import celery_app

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


def _to_response(
    schedule: Schedule,
    *,
    owner_name: str | None = None,
    run_stats: dict | None = None,
) -> ScheduleResponse:
    """`run_stats` (Task 5 residual, spec §B6) is this ONE schedule's own
    `{"last_run_duration_seconds", "runs_last_7_days"}` slice of
    `schedule_service.schedule_run_stats`'s batched result — the caller looks
    it up once per row from that single query's output, never re-queries
    here. `None` (every non-list caller — `get_schedule`/`update_schedule`/
    etc.) renders the un-enriched defaults (`schedule_run_stats` is a list-
    page-only cost; see `list_schedules` below)."""
    run_stats = run_stats or {}
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
        last_run_duration_seconds=run_stats.get("last_run_duration_seconds"),
        runs_last_7_days=run_stats.get("runs_last_7_days", 0),
        owner_name=owner_name,
        created_via=schedule.created_via,
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


@router.get("", response_model=ScheduleListResponse)
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
    would otherwise reveal this).

    Task 5 residual (spec §B6): also carries each row's own
    `last_run_duration_seconds`/`runs_last_7_days`/`owner_name` and the page's
    tenant-wide "Last 7 days" tile totals — THREE queries total for the whole
    page regardless of schedule count (`schedule_run_stats`,
    `tenant_run_totals_7d`, `owner_names`), never one per row.
    """
    schedules = await schedule_service.list_schedules(db, user.tenant_id)
    schedule_ids = [s.id for s in schedules]
    owner_ids = [s.owner_id for s in schedules if s.owner_id is not None]

    run_stats = await schedule_service.schedule_run_stats(db, user.tenant_id, schedule_ids)
    names = await schedule_service.owner_names(db, owner_ids)
    total, failed = await schedule_service.tenant_run_totals_7d(db, user.tenant_id)

    return ScheduleListResponse(
        schedules=[
            _to_response(
                s,
                owner_name=names.get(s.owner_id) if s.owner_id else None,
                run_stats=run_stats.get(s.id),
            )
            for s in schedules
        ],
        runs_last_7_days_total=total,
        runs_last_7_days_failed=failed,
    )


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
    correlation_id = _correlation_id(request)

    if body.instruction:
        # Item 5 (gate fix): the shared create path (also used by the MCP
        # `schedule.create` tool's instruction branch) owns the entitlement
        # check + compile + persist — see its own docstring.
        try:
            schedule = await schedule_service.create_scheduled_job(
                db,
                tenant_id=user.tenant_id,
                body=body,
                actor_id=user.id,
                created_via="page",
            )
        except schedule_service.QuotaExceeded as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except Clarification as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"clarification": exc.question},
            ) from exc

        await db.commit()
        await db.refresh(schedule)
        return _to_response(schedule)

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
        owner_id=user.id,
        created_via="page",
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

    # Item 3 (gate fix): a Scheduled-Job-only edit must not act on a
    # pre-Slice-2 `sync|report|recon` row — that row type has no
    # `plan_json`/compiler pipeline at all, so `instruction` and
    # `discard_pending` are meaningless there. Direct field edits
    # (name/cron_expression/timezone/delivery/budget/catch_up) stay allowed
    # on every schedule type, exactly as before.
    if (body.instruction is not None or body.discard_pending) and schedule.schedule_type != "job":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not a scheduled job")

    correlation_id = _correlation_id(request)
    changed_fields: dict = {}
    cron_or_tz_changed = False

    if body.name is not None:
        schedule.name = body.name
        changed_fields["name"] = body.name
    if body.cron_expression is not None:
        schedule.cron_expression = body.cron_expression
        changed_fields["cron_expression"] = body.cron_expression
        cron_or_tz_changed = True
    if body.timezone is not None:
        schedule.timezone = body.timezone
        changed_fields["timezone"] = body.timezone
        cron_or_tz_changed = True
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

    # Item 2 (gate fix): `approve_schedule`/`resume_schedule` both recompute
    # `next_run_at` when their own preconditions hold — a bare cron/timezone
    # edit on an already-approved, active, unpaused schedule must mirror that,
    # or the schedule keeps firing at the STALE fire time until the next
    # approve/resume happens to touch it (which may be never, for a schedule
    # nobody re-approves after this edit). Item 4 (delta gate fix): the
    # shared `recompute_next_run_at` also refuses to touch `next_run_at`
    # while a retry is pending (`retry_job_id` set) — `cron_or_tz_changed`
    # stays the trigger for whether to call it at all.
    if cron_or_tz_changed:
        schedule_service.recompute_next_run_at(schedule, now=datetime.now(timezone.utc))

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

    # Item 3 (gate fix): approve is a Scheduled-Job-only concept — a legacy
    # row's `plan_status`/`plan_json` don't mean anything (always None), so
    # without this check a legacy row falls through to the generic "no
    # pending plan" 400 below instead of the clearer "wrong row type" signal.
    if schedule.schedule_type != "job":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not a scheduled job")

    if schedule.pending_plan_json:
        schedule.plan_json = schedule.pending_plan_json
        schedule.pending_plan_json = None
        schedule.pending_plan_reason = None
    elif schedule.plan_status != "pending_approval" or not schedule.plan_json:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No pending plan to approve")

    schedule.plan_status = "approved"
    schedule.plan_version += 1

    # Item 4 (delta gate fix): the shared helper refuses to touch
    # `next_run_at` while a retry is pending (`retry_job_id` set) — approving
    # (or re-approving) a schedule must not clobber that 15-minutes-later due
    # time with the schedule's normal next occurrence.
    schedule_service.recompute_next_run_at(schedule, now=datetime.now(timezone.utc))

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


@router.post("/{schedule_id}/run", response_model=ScheduleRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def run_schedule(
    schedule_id: uuid.UUID,
    body: ScheduleRunRequest,
    user: Annotated[User, Depends(require_permission("schedules.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Enqueue one run now (spec §B5: "enqueues one run now; records the plan
    version used") — via Celery, NOT executed inline on this request's own
    session (residual fix): a real Inventory Aging run can take minutes,
    which used to hold the HTTP request open long enough for nginx to cut it.

    This route creates the `jobs` row itself — status `pending` — so the
    `202` response can carry its real id immediately, then dispatches
    `tasks.scheduled_jobs_run_now` (`app.workers.tasks.scheduled_jobs.
    run_schedule_now_task`) with that row's id; `run_schedule_now`'s
    `existing_job_id` param (see its own docstring) makes the task REUSE this
    SAME row rather than inserting a second one. The HITL precondition checks
    below still run synchronously, on THIS request — a blocked run never
    creates a row or enqueues a task, matching the pre-existing 409 contract.

    The row's `parameters["plan"]` is a SNAPSHOT of the plan THIS request
    validated (`plan_to_run`, review finding, MAJOR): an instruction edit or
    discard landing between this enqueue and the Celery task's execution used
    to silently change what runs, because `run_schedule_now` re-read
    `schedule.plan_json`/`pending_plan_json` LIVE when the task actually
    executed. `run_schedule_now` replays this snapshot instead — the HITL
    `plan_status` gate and the `plan_version_used` bookkeeping below are
    unaffected, and still read the schedule row live.

    Item 7 (gate fix): the precondition checks + row-creation + audit body
    now live in `schedule_service.enqueue_run`, shared with the MCP
    `schedule.run` tool's `execute_run` — this route and that tool cannot
    drift on the HITL gate or the plan-snapshot behaviour any more.
    """
    schedule = await _get_or_404(db, schedule_id, user.tenant_id)

    try:
        job = await schedule_service.enqueue_run(
            db,
            schedule=schedule,
            tenant_id=user.tenant_id,
            actor_id=user.id,
            use_pending=body.use_pending,
        )
    except schedule_service.NoPlanToRun as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except schedule_service.PlanNotApproved as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    job_id = job.id
    await db.commit()

    celery_app.send_task(
        "tasks.scheduled_jobs_run_now",
        kwargs={
            "schedule_id": str(schedule_id),
            "tenant_id": str(user.tenant_id),
            "use_pending": body.use_pending,
            "actor_id": str(user.id),
            "job_id": str(job_id),
        },
        queue="sync",
    )

    return ScheduleRunResponse(jobs_id=str(job_id), status="queued", reason=None, outputs={})


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

    # Item 4 (delta gate fix): the shared helper refuses to touch
    # `next_run_at` while a retry is pending (`retry_job_id` set) — resuming
    # a schedule must not clobber that 15-minutes-later due time with the
    # schedule's normal next occurrence.
    schedule_service.recompute_next_run_at(schedule, now=datetime.now(timezone.utc))

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
