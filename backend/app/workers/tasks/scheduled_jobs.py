"""Executor + Beat sweep for the Scheduled Jobs platform (Slice 2, Task 3).

Spec: docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md
§B4 (binding):

    Beat entry `scheduled-jobs-sweep` every minute -> `scheduled_jobs_sweep_all`
    (fan out per active tenant, like `report_auto_refresh_all`) -> `run_due_jobs(tenant_id)`:
    `SELECT ... FOR UPDATE SKIP LOCKED` on due schedules (`next_run_at <= now`, active,
    approved plan, not paused); catch-up = run once if a run was missed; compute
    `next_run_at` with `croniter` in the schedule's timezone. Each run = one `jobs` row
    (job_type `scheduled_job`, parameters incl. schedule id + plan version + period key,
    correlation id) via the instrumented task base; steps execute in order with the
    run's budget (bytes scanned, seconds, usd) enforced between steps; the run ends
    with a reason enum stored in `result_summary.reason` (`done|budget|stall|error|
    blocked`); on `error`: schedule one retry 15 min later (a `jobs` row with
    `attempt=2`), then `paused_at` + `pause_reason` + notify the owner (audit event +
    the existing notification path if one exists on main; otherwise the audit event
    and the page badge are the notification). WRITE steps: audit `started` with the
    idempotency key before the call.

THE AGENT NEVER RUNS HERE. This module only ever replays an already-compiled,
already-approved `plan_json` (or, for a "run once with this change" manual trigger,
`pending_plan_json`) — no LLM call anywhere below. That boundary is enforced by
construction: nothing in this file imports `app.services.jobs.compiler`.

Two choke points, one registry (registry.py's own docstring): the compiler rejects
an unknown step type at COMPILE time; `_run_steps` below is the SECOND choke point —
every step's type is looked up in `STEP_REGISTRY` FRESH, on every step, never cached
at import or call-start, so (a) a step type retired from the registry after a plan
was compiled is rejected here exactly the same way, and (b) a test can monkeypatch
`STEP_REGISTRY[...] = StepSpec(...)` with a fake executor and this loop picks it up
with no other change — which is how `tests/jobs/test_executor.py` and the seeded-
tenant e2e drive this module deterministically, without live BigQuery/Drive/
WeasyPrint credentials.

Reason enum (agent-graph.md #5 / spec §B4): `done | budget | stall | error | blocked`,
written to `jobs.result_summary["reason"]`. `blocked` is this module's one addition
to the repo-wide four (`rolling_period_compose.py`'s REASON_* constants) — a
scheduled run can end in a clean, EXPECTED non-failure that is neither "succeeded"
nor "broken": `report.report_delivery.DeliveryUnavailable` (no Google Sheets
connector configured yet) is spec §A5's own example ("the run ends blocked, never
500"), and this module maps it to `blocked` rather than `error` so a paused/retried
job and a not-yet-configured one read differently on the page and in the ops digest.

Idempotency + audit-before-call (agent-graph.md #10): a WRITE step's `started` audit
event is written and COMMITTED — not merely flushed — strictly before its executor
is invoked, so a crash between "we started this write" and "the write confirmed" is
recoverable (a retry can see the started event and know the call may have gone out).
This is a deliberate, one-off exception to the "service flushes, endpoint commits
once" convention (`.claude/rules/sqlalchemy-fastapi.md`) — durability is the entire
point of that particular commit.

Claim vs. run (the SKIP LOCKED lock is short-lived, not held for the run's duration):
`_claim_due_schedules` does the `SELECT ... FOR UPDATE SKIP LOCKED`, immediately
advances `next_run_at` past `now` and marks the row `running`, and COMMITS — a
standard job-queue "claim" pattern. This is what actually gives "two concurrent
sweeps -> one `jobs` row" (spec/tests): whichever transaction's SELECT lands first
holds the row until its commit; the other transaction's SKIP LOCKED select either
skips the still-locked row outright, or (if it runs after the first commit) simply
no longer matches the `next_run_at <= now` predicate, because the winner already
moved it into the future. Either way, at most one claim succeeds. The run itself
(`run_schedule_now`, which can take a while — Drive uploads, BigQuery, WeasyPrint)
then proceeds WITHOUT holding any Postgres row lock, so a slow report never blocks
the next tenant's sweep tick.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import set_tenant_context
from app.models.job import Job
from app.models.pipeline import Schedule
from app.models.tenant import Tenant
from app.services import audit_service
from app.services.jobs.registry import STEP_REGISTRY, StepContext, StepExecutionError
from app.services.schedule_service import compute_next_run
from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

__all__ = [
    "REASON_DONE",
    "REASON_BUDGET",
    "REASON_STALL",
    "REASON_ERROR",
    "REASON_BLOCKED",
    "RunOutcome",
    "compute_next_run",
    "run_due_jobs",
    "run_schedule_now",
    "scheduled_jobs_sweep_all",
    "scheduled_jobs_sweep_tenant",
]

# stdlib logger, and every call site passes context via `extra=` — NOT structlog
# kwargs. Celery hijacks the root logger; see report_auto_refresh.py's identical
# note (project_report_auto_refresh_first_organic_run_failed).
logger = logging.getLogger(__name__)

#: Terminal reasons. Mirrors agent-graph.md #5 plus this module's own `blocked`
#: (see module docstring) — do not add a sixth without deciding how the page's
#: run pill and the ops digest should route it.
REASON_DONE = "done"
REASON_BUDGET = "budget"
REASON_STALL = "stall"
REASON_ERROR = "error"
REASON_BLOCKED = "blocked"

RETRY_DELAY_MINUTES = 15  # spec §B4: "retry once after 15 minutes"
RETRY_MAX_ATTEMPTS = 2  # attempt 1 (the original due run) + attempt 2 (the one retry)

# `Schedule.last_run_status` sentinel meaning "attempt 1 failed, a retry is due at
# next_run_at" — read back by `run_schedule_now` to decide whether THIS run is the
# retry (attempt 2), and by nothing else; it is never a value the page needs to
# render specially (the page reads `paused_at`/`pause_reason` for that state).
_RETRY_PENDING = "retry_pending"


@dataclass
class RunOutcome:
    """What one `run_schedule_now` call produced. `outputs` is the SAME
    JSON-safe dict persisted at `jobs.result_summary["outputs"]` — keyed by
    plan step id, one distilled artifact per step that actually ran (see
    `_distill_artifact`)."""

    reason: str
    jobs_row_id: uuid.UUID | None
    outputs: dict[str, Any]


# ---------------------------------------------------------------------------
# Due-time computation — `compute_next_run` lives in `schedule_service.py`
# (imported above) since Task 5's approve/edit endpoints need the SAME
# formula the sweep uses below, not a second copy; re-exported here (see
# `__all__`) so `from app.workers.tasks.scheduled_jobs import compute_next_run`
# — this module's own documented interface — still works unchanged.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Testing seam — see the concurrency test in tests/jobs/test_executor.py.
# ---------------------------------------------------------------------------


async def _default_claim_sync_hook() -> None:
    """No-op in production (an awaited no-op costs nothing on the hot path).
    A concurrency test monkeypatches THIS module attribute to pause one claim
    transaction right after its `SELECT ... FOR UPDATE SKIP LOCKED` and before
    its commit, so a second, genuinely concurrent claim attempt (a separate
    engine/connection) can be driven to observe the row as locked — the only
    way to exercise real Postgres SKIP LOCKED semantics rather than merely the
    post-commit `next_run_at` predicate. This is the ONE testing seam in the
    claim path."""
    return None


_claim_sync_hook: Callable[[], Awaitable[None]] = _default_claim_sync_hook


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


def _due_predicate(tenant_id: uuid.UUID, now: datetime):
    return (
        Schedule.tenant_id == tenant_id,
        Schedule.schedule_type == "job",
        Schedule.is_active.is_(True),
        Schedule.plan_status == "approved",
        Schedule.paused_at.is_(None),
        Schedule.next_run_at.isnot(None),
        Schedule.next_run_at <= now,
    )


@dataclass
class _Claim:
    schedule_id: uuid.UUID
    due_at: datetime
    plan_version: int
    run: bool  # False only for a catch_up="skip" row whose window was missed
    attempt: int  # 1, or 2 for the one 15-minutes-later retry (spec §B4)


async def _claim_due_schedules(db: AsyncSession, tenant_id: uuid.UUID, now: datetime) -> list[_Claim]:
    """`SELECT ... FOR UPDATE SKIP LOCKED` the tenant's due schedules, advance
    each one's `next_run_at` past `now` and mark it `running` (or `skipped` —
    see below), then COMMIT immediately (module docstring: "Claim vs. run").

    Catch-up (spec §0.6 / §B1 `catch_up`): a schedule whose `next_run_at` was
    already due by MORE than one full cron interval has a missed window.
    `catch_up="once"` (the default) still runs it — exactly once, never once
    per missed interval, because this function claims each due ROW once
    regardless of how far in the past its `next_run_at` was; there is no
    per-missed-period loop to accidentally run twice. `catch_up="skip"`
    instead skips a MISSED window's run entirely (still advances
    `next_run_at`, still marks the row so the page can show it) — a schedule
    that is merely on time (not missed) always runs either way.
    """
    rows = (
        (await db.execute(select(Schedule).where(*_due_predicate(tenant_id, now)).with_for_update(skip_locked=True)))
        .scalars()
        .all()
    )

    await _claim_sync_hook()

    claims: list[_Claim] = []
    for row in rows:
        due_at = row.next_run_at
        # Captured BEFORE `last_run_status` is overwritten below — this is the
        # ONLY place attempt-2 (the retry) is distinguishable from a fresh due
        # run, since `run_schedule_now` no longer sees the pre-claim value.
        attempt = 2 if row.last_run_status == _RETRY_PENDING else 1
        try:
            next_after_due = compute_next_run(row.cron_expression, row.timezone, after=due_at)
            missed = next_after_due <= now
            row.next_run_at = compute_next_run(row.cron_expression, row.timezone, after=now)
        except Exception:
            logger.warning(
                "scheduled_jobs.claim.next_run_at_compute_failed",
                exc_info=True,
                extra={"schedule_id": str(row.id)},
            )
            continue

        skip = row.catch_up == "skip" and missed
        row.last_run_status = "skipped" if skip else "running"
        claims.append(
            _Claim(schedule_id=row.id, due_at=due_at, plan_version=row.plan_version, run=not skip, attempt=attempt)
        )

    if rows:
        await db.commit()
    return claims


# ---------------------------------------------------------------------------
# Run one schedule
# ---------------------------------------------------------------------------


def _distill_artifact(artifact: dict) -> dict:
    """A JSON-safe subset of one step's artifact, for `jobs.result_summary`.
    registry.py's own docstring assigns this to the run loop, not the
    registry: an artifact may hold live objects (a `Report` ORM row, raw PDF/
    Excel bytes) that never belong in a persisted JSON column."""
    distilled: dict[str, Any] = {}
    for key, value in artifact.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            distilled[key] = value
        elif isinstance(value, bytes):
            distilled[key] = {"bytes": len(value)}
        elif isinstance(value, dict):
            distilled[key] = value
        elif isinstance(value, list):
            distilled[key] = value
        # anything else (e.g. a live ORM row) is dropped — it is still available
        # in-memory via ctx.artifacts for a later step in THIS run.
    return distilled


async def _run_steps(
    db: AsyncSession,
    *,
    ctx: StepContext,
    steps: list[dict],
    budget: dict,
    correlation_id: str,
    job_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    actor_type: str,
) -> tuple[str, dict[str, Any], str | None]:
    """Replay `steps` in order. Returns (reason, outputs, detail)."""
    from app.services.report.report_delivery import DeliveryUnavailable

    outputs: dict[str, Any] = {}
    usage = {"bytes_scanned": 0, "seconds": 0.0}
    started_at = time.monotonic()

    for step in steps:
        step_id = step.get("id")
        step_type = step.get("type")
        spec = STEP_REGISTRY.get(step_type)  # looked up FRESH — see module docstring
        if spec is None:
            return REASON_ERROR, outputs, f"step {step_id!r}: unknown step type {step_type!r} — not in the registry"

        params = step.get("params") or {}

        if spec.kind == "write":
            idem_key = spec.idempotency(ctx, params)
            await set_tenant_context(db, str(tenant_id))
            await audit_service.log_event(
                db,
                tenant_id=tenant_id,
                category="jobs",
                action=f"{step_type}.started",
                actor_id=actor_id,
                actor_type=actor_type,
                resource_type="schedule_step",
                resource_id=step_id,
                correlation_id=correlation_id,
                job_id=job_id,
                payload={"step_type": step_type, "idempotency_key": idem_key},
            )
            # COMMIT, not flush — durable BEFORE the call (agent-graph.md #10);
            # see the module docstring's "Idempotency + audit-before-call".
            await db.commit()
            await set_tenant_context(db, str(tenant_id))

        try:
            artifact = await spec.executor(ctx, params)
        except DeliveryUnavailable as exc:
            return REASON_BLOCKED, outputs, str(exc)
        except StepExecutionError as exc:
            return REASON_ERROR, outputs, str(exc)
        except Exception as exc:  # an executor's own unexpected failure
            logger.exception("scheduled_jobs.step_failed", extra={"step_id": step_id, "step_type": step_type})
            return REASON_ERROR, outputs, f"{type(exc).__name__}: {exc}"

        ctx.artifacts[step_id] = artifact
        outputs[step_id] = _distill_artifact(artifact)

        usage["bytes_scanned"] += int(artifact.get("bytes_processed") or 0)
        usage["seconds"] = time.monotonic() - started_at

        limit_bytes = budget.get("bytes_scanned")
        limit_seconds = budget.get("seconds")
        over_budget = (limit_bytes is not None and usage["bytes_scanned"] > limit_bytes) or (
            limit_seconds is not None and usage["seconds"] > limit_seconds
        )
        if over_budget:
            return REASON_BUDGET, outputs, None

    return REASON_DONE, outputs, None


async def run_schedule_now(
    db: AsyncSession,
    schedule_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None = None,
    actor_type: str = "system",
    use_pending: bool = False,
    due_at: datetime | None = None,
    now: datetime | None = None,
    attempt: int = 1,
) -> RunOutcome:
    """Run one schedule ONE time: one `jobs` row, plan steps replayed in order,
    schedule bookkeeping updated, one commit at the end (plus the write-step
    audit-before-call commits inside `_run_steps`).

    `use_pending=True` (spec §B5 "Run once with this change") replays
    `pending_plan_json` instead of the approved `plan_json`, WITHOUT bumping
    `plan_version` (only `POST .../approve` does that) — the job's own
    `parameters["plan_version"]` instead records what version the pending
    plan WOULD become on approval (`plan_version + 1`), which is what "records
    the plan version used" (spec §B4) means for a run that used a
    not-yet-approved plan.

    Retry-then-pause (spec §B4): `attempt` (1, or 2 for the one 15-minutes-
    later retry) is a caller-supplied fact, not derived here — `run_due_jobs`
    reads it off the schedule row's `last_run_status` DURING the claim, before
    the claim overwrites that same field to `"running"` (see
    `_claim_due_schedules`); by the time this function runs, the row no longer
    carries that information itself. A manual "Run now" (Task 5's API) is
    always attempt 1 — it is never the scheduled retry.
    """
    now = now or datetime.now(timezone.utc)
    due_at = due_at or now

    await set_tenant_context(db, str(tenant_id))
    row = (
        await db.execute(select(Schedule).where(Schedule.id == schedule_id, Schedule.tenant_id == tenant_id))
    ).scalar_one()

    plan_json = row.pending_plan_json if use_pending else row.plan_json
    plan_version_used = (row.plan_version + 1) if use_pending else row.plan_version

    if not plan_json or not (plan_json.get("steps")):
        row.last_run_status = REASON_BLOCKED
        row.last_run_at = now
        await set_tenant_context(db, str(tenant_id))
        await audit_service.log_event(
            db,
            tenant_id=tenant_id,
            category="jobs",
            action="jobs.run.blocked",
            actor_id=actor_id,
            actor_type=actor_type,
            resource_type="schedule",
            resource_id=str(schedule_id),
            payload={"detail": "no compiled plan to run", "use_pending": use_pending},
            status="error",
        )
        await db.commit()
        return RunOutcome(reason=REASON_BLOCKED, jobs_row_id=None, outputs={})

    period_key = due_at.astimezone(ZoneInfo(row.timezone)).date().isoformat()
    correlation_id = str(uuid.uuid4())

    job = Job(
        tenant_id=tenant_id,
        job_type="scheduled_job",
        status="running",
        correlation_id=correlation_id,
        started_at=now,
        parameters={
            "schedule_id": str(schedule_id),
            "plan_version": plan_version_used,
            "period_key": period_key,
            "attempt": attempt,
            "use_pending": use_pending,
        },
    )
    db.add(job)
    await db.flush()

    await audit_service.log_event(
        db,
        tenant_id=tenant_id,
        category="jobs",
        action="jobs.run.start",
        actor_id=actor_id,
        actor_type=actor_type,
        resource_type="job",
        resource_id=str(job.id),
        correlation_id=correlation_id,
        job_id=job.id,
        payload={"schedule_id": str(schedule_id), "plan_version": plan_version_used, "attempt": attempt},
    )
    await db.commit()

    ctx = StepContext(
        job_id=schedule_id,
        run_id=job.id,
        tenant_id=tenant_id,
        db=db,
        budget=dict(row.budget_json or {}),
        period_key=period_key,
        actor_type=actor_type,
        actor_id=actor_id,
    )

    reason, outputs, detail = await _run_steps(
        db,
        ctx=ctx,
        steps=plan_json.get("steps") or [],
        budget=row.budget_json or {},
        correlation_id=correlation_id,
        job_id=job.id,
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_type=actor_type,
    )

    await set_tenant_context(db, str(tenant_id))
    row = (
        await db.execute(select(Schedule).where(Schedule.id == schedule_id, Schedule.tenant_id == tenant_id))
    ).scalar_one()
    job = await db.get(Job, job.id)

    job.status = "completed" if reason in (REASON_DONE, REASON_BUDGET, REASON_BLOCKED) else "failed"
    job.completed_at = datetime.now(timezone.utc)
    job.result_summary = {"reason": reason, "outputs": outputs, "detail": detail}
    if detail:
        job.error_message = detail

    row.last_run_at = now
    row.last_run_status = reason

    if reason == REASON_ERROR:
        if attempt >= RETRY_MAX_ATTEMPTS:
            row.paused_at = datetime.now(timezone.utc)
            row.pause_reason = f"paused after {attempt} failed attempts: {detail}"[:1000]
            row.last_run_status = "paused"
            await audit_service.log_event(
                db,
                tenant_id=tenant_id,
                category="jobs",
                action="jobs.paused",
                actor_id=None,
                actor_type="system",
                resource_type="schedule",
                resource_id=str(schedule_id),
                correlation_id=correlation_id,
                job_id=job.id,
                payload={
                    "reason": row.pause_reason,
                    "owner_id": str(row.owner_id) if row.owner_id else None,
                },
                status="error",
            )
        else:
            row.next_run_at = now + timedelta(minutes=RETRY_DELAY_MINUTES)
            row.last_run_status = _RETRY_PENDING

    await audit_service.log_event(
        db,
        tenant_id=tenant_id,
        category="jobs",
        action="jobs.run.complete",
        actor_id=actor_id,
        actor_type=actor_type,
        resource_type="job",
        resource_id=str(job.id),
        correlation_id=correlation_id,
        job_id=job.id,
        payload={"reason": reason},
        status="success" if reason == REASON_DONE else "error",
    )
    await db.commit()

    return RunOutcome(reason=reason, jobs_row_id=job.id, outputs=outputs)


# ---------------------------------------------------------------------------
# Per-tenant sweep
# ---------------------------------------------------------------------------


async def run_due_jobs(db: AsyncSession, tenant_id: uuid.UUID, *, now: datetime | None = None) -> dict:
    """Claim + run every due schedule of one tenant. Per-schedule isolation:
    one schedule's crash does not abort the rest of the tenant's batch
    (matches `sweep_tenant_reports`/`sweep_tenant_series`'s established
    per-item try/except + rollback pattern)."""
    now = now or datetime.now(timezone.utc)
    stats = {"tenant_id": str(tenant_id), "due": 0, "ran": 0, "skipped": 0, "failed": 0, "reason": REASON_DONE}

    await set_tenant_context(db, str(tenant_id))
    claims = await _claim_due_schedules(db, tenant_id, now)
    stats["due"] = len(claims)

    for claim in claims:
        if not claim.run:
            stats["skipped"] += 1
            continue
        try:
            outcome = await run_schedule_now(
                db,
                claim.schedule_id,
                tenant_id=tenant_id,
                actor_id=None,
                actor_type="system",
                due_at=claim.due_at,
                now=now,
                attempt=claim.attempt,
            )
            stats["ran"] += 1
            if outcome.reason == REASON_ERROR:
                stats["failed"] += 1
        except Exception:
            stats["failed"] += 1
            logger.exception(
                "scheduled_jobs.run_due_jobs.schedule_failed", extra={"schedule_id": str(claim.schedule_id)}
            )
            try:
                await db.rollback()
            except Exception:
                logger.warning("scheduled_jobs.rollback_failed", exc_info=True)

    if stats["failed"]:
        stats["reason"] = REASON_ERROR
    logger.info("scheduled_jobs.sweep_completed", extra=stats)
    return stats


# --- Celery glue -------------------------------------------------------------------------


async def collect_and_dispatch(db: AsyncSession) -> dict:
    """Fan-out: one `tasks.scheduled_jobs_sweep` per ACTIVE tenant — the same
    shape as `report_auto_refresh_all`/`rolling_period_compose_all`."""
    if not settings.SCHEDULED_JOBS_ENABLED:
        return {"enabled": False, "dispatched": 0}
    tenant_ids = (await db.execute(select(Tenant.id).where(Tenant.is_active.is_(True)))).scalars().all()
    stats = {"enabled": True, "dispatched": 0, "failed": 0}
    for tenant_id in tenant_ids:
        try:
            celery_app.send_task("tasks.scheduled_jobs_sweep", kwargs={"tenant_id": str(tenant_id)}, queue="sync")
            stats["dispatched"] += 1
        except Exception:
            stats["failed"] += 1
            logger.exception("scheduled_jobs_sweep_all.dispatch_failed", extra={"tenant_id": str(tenant_id)})
    logger.info("scheduled_jobs_sweep_all.completed", extra=stats)
    return stats


@celery_app.task(base=InstrumentedTask, name="tasks.scheduled_jobs_sweep_all", queue="sync")
def scheduled_jobs_sweep_all():
    """Beat entry point (every minute — celery_app.py). Opens its own session;
    logic lives in collect_and_dispatch()."""
    import asyncio

    from app.core.database import worker_async_session

    async def _run() -> dict:
        async with worker_async_session() as db:
            return await collect_and_dispatch(db)

    # No in-task retry: the next minute's tick is the retry (house convention).
    return asyncio.run(_run())


@celery_app.task(base=InstrumentedTask, name="tasks.scheduled_jobs_sweep", queue="sync")
def scheduled_jobs_sweep_tenant(tenant_id: str):
    """Per-tenant sweep task. One tenant per task — no cross-tenant session reuse."""
    import asyncio

    from app.core.database import set_tenant_context_session, worker_async_session

    async def _run() -> dict:
        async with worker_async_session() as db:
            # Session-scoped SET (not SET LOCAL): run_schedule_now commits
            # repeatedly mid-run (the write-step audit-before-call commit, plus
            # the final commit), which would clear a transaction-scoped GUC.
            # Safe ONLY because this engine is disposable and never returns to
            # an app pool (report_auto_refresh_tenant/rolling_period_compose_tenant
            # use the identical pattern for the identical reason).
            await set_tenant_context_session(db, tenant_id)
            return await run_due_jobs(db, uuid.UUID(tenant_id))

    return asyncio.run(_run())
