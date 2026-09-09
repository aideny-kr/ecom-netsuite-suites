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

One `jobs` row per run, via "the instrumented task base" (spec §B4): honored in
spirit, not literally. `run_schedule_now` writes the same `jobs` table with the same
field conventions `InstrumentedTask` (`base_task.py`) uses — `job_type`, `status`,
`correlation_id`, `parameters`, `result_summary`, `started_at`/`completed_at` — via
direct async-session ORM writes, because this function must be directly callable
against an async session both from this module's own per-tenant sweep loop AND,
per spec, from Task 5's future request-scoped "Run now" endpoint; `InstrumentedTask`'s
`before_start`/`on_success`/`on_failure` hooks only fire around an actual Celery task
dispatch, and wrapping every individual schedule's run in its own Celery task would
trade this module's straightforward per-tenant sweep loop for one Celery task per
schedule per minute. The residual gap that design choice leaves open — a crash
OUTSIDE `_run_steps`'s own rollback-before-return protection (e.g. re-fetching the
`Schedule`/`Job` rows afterward) has no `InstrumentedTask.on_failure` to fall back
on — is closed explicitly: `run_schedule_now` retries `_finalize_run` once, against a
freshly rolled-back session, forcing `reason=error` on the retry, so the SAME
retry-then-pause bookkeeping every other error path uses runs on that retry rather
than leaving the `jobs` row it created stuck at `status="running"` (review finding).
That retry is itself guarded too (a second review finding): if the SAME `_finalize_run`
call fails AGAIN — the ORM re-fetch path apparently broken, not just flaky once — a
third ORM attempt is not made. Instead a minimal, non-ORM raw SQL `UPDATE` marks the
`jobs` row `status='failed'` (the same vocabulary every other writer of that column
uses, so the run still shows in failed-job filters) and the `schedules` row
`last_run_status='error'` by id, best-effort audits the failure, and returns. This is
best effort, not a guarantee: if that single UPDATE-by-id itself raises (extremely
unlikely, but possible if the database is unreachable) the failure is logged and not
retried again, and the `jobs` row would remain at `status="running"` until the
startup stale-job cleanup marks it failed. Unlike the normal error path, this
fallback does NOT schedule the usual 15-minutes-later retry-then-pause cycle, since
that needs the very ORM objects this fallback exists because it could not get.

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

The `jobs` row itself is created in THAT SAME claim transaction, for every
runnable claim (review finding, MAJOR): before this fix, the claim committed
`next_run_at` advanced + `running` with no `jobs` row yet, so a worker crash
between that commit and `run_schedule_now`'s own insert dropped the occurrence
with no record anywhere. `run_due_jobs` passes the pre-created row's id as
`existing_job_id` so `run_schedule_now` updates it in place rather than
inserting a second one.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
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
    "run_schedule_now_task",
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

#: Spec §B4: the run budget is "(bytes scanned, seconds, usd) enforced between
#: steps", but no v1 step type (registry.py) reports a `cost_usd` figure
#: directly in its artifact -- only `bigquery_sql` reports `bytes_processed`
#: at all. Derive a run's usd usage from that the SAME way
#: `app.services.bigquery_service.estimate_query_cost` already prices a
#: BigQuery dry-run (`estimated_bytes / 1e12 * 5`) -- keep this literal in
#: sync with that function's if the BigQuery on-demand rate ever changes.
#: If a future step type starts reporting cost directly, extend `usage["usd"]`
#: below to also sum that field rather than replacing this derivation.
_BIGQUERY_USD_PER_BYTE = 5.0 / 1_000_000_000_000

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
    # A pre-created `jobs` row (status="pending"), inserted in the SAME
    # transaction as the claim itself -- see _claim_due_schedules. `None`
    # only for a `run=False` (skipped) claim, which never runs a job at all.
    job_id: uuid.UUID | None = None


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
    that is merely on time (not missed) always runs either way. `skip` never
    applies to the one 15-minutes-later retry (`attempt == 2`): a retry is
    never "missed" in the catch-up sense, and letting `skip` fire there would
    silently swallow the original failure instead of ever running the retry.

    A `compute_next_run` failure (bad `cron_expression`/`timezone`) PAUSES the
    row right here — `paused_at`/`pause_reason`/`last_run_status="paused"`,
    `next_run_at=None`, plus the same owner-notification audit event the
    retry-then-pause path (`run_schedule_now`) uses — rather than `continue`
    past it: a `continue` left `next_run_at <= now` forever, so the row was
    re-claimed and this same exception re-logged every minute, indefinitely,
    with nothing ever recorded for a human to see (review finding, MAJOR).
    """
    rows = (
        (await db.execute(select(Schedule).where(*_due_predicate(tenant_id, now)).with_for_update(skip_locked=True)))
        .scalars()
        .all()
    )

    await _claim_sync_hook()

    claims: list[_Claim] = []
    pending_jobs: list[tuple[_Claim, Job]] = []
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
        except Exception as exc:
            # A bad cron_expression/timezone must PAUSE the schedule, never
            # silently `continue` (review finding, MAJOR): `continue` left
            # `next_run_at <= now` untouched, so the sweep re-claimed this
            # SAME row every minute forever with no record anywhere that
            # anything was wrong. Pausing here uses the identical fields/audit
            # shape the retry-then-pause path (below, in run_schedule_now)
            # uses, so the page and the ops digest render this exactly like
            # any other paused schedule.
            logger.warning(
                "scheduled_jobs.claim.next_run_at_compute_failed",
                exc_info=True,
                extra={"schedule_id": str(row.id)},
            )
            row.paused_at = now
            row.pause_reason = f"paused: schedule cannot be computed ({type(exc).__name__}: {exc})"[:1000]
            row.last_run_status = "paused"
            row.next_run_at = None
            await audit_service.log_event(
                db,
                tenant_id=tenant_id,
                category="jobs",
                action="jobs.paused",
                actor_id=None,
                actor_type="system",
                resource_type="schedule",
                resource_id=str(row.id),
                payload={
                    "reason": row.pause_reason,
                    "owner_id": str(row.owner_id) if row.owner_id else None,
                },
                status="error",
            )
            continue

        # `skip` applies ONLY to a fresh attempt-1 claim (review finding,
        # MAJOR): the pending attempt-2 retry is never "missed" in the
        # catch-up sense, and letting `skip` fire there marked the retry
        # `skipped`/`run=False` -- retry-then-pause never actually ran the
        # retry, silently swallowing the original failure forever.
        skip = row.catch_up == "skip" and missed and attempt == 1
        row.last_run_status = "skipped" if skip else "running"
        claim = _Claim(schedule_id=row.id, due_at=due_at, plan_version=row.plan_version, run=not skip, attempt=attempt)

        if claim.run:
            # The `jobs` row is created HERE, inside the SAME transaction as
            # the claim (review finding, MAJOR): before this fix, the claim
            # committed `next_run_at` advanced + `running` with NO `jobs` row
            # yet -- a worker crash between that commit and `run_schedule_now`'s
            # own insert dropped the occurrence with no record anywhere. A
            # crash now leaves a visible `pending` row against a `running`
            # schedule instead of nothing. `run_due_jobs` passes this id as
            # `existing_job_id` so `run_schedule_now` reuses it rather than
            # inserting a second row.
            pending_job = Job(
                tenant_id=tenant_id,
                job_type="scheduled_job",
                status="pending",
                parameters={"schedule_id": str(row.id), "attempt": attempt, "due_at": due_at.isoformat()},
            )
            db.add(pending_job)
            pending_jobs.append((claim, pending_job))

        claims.append(claim)

    if rows:
        if pending_jobs:
            await db.flush()  # need each pending_job.id before COMMIT below
            for claim, pending_job in pending_jobs:
                claim.job_id = pending_job.id
        await db.commit()
    return claims


# ---------------------------------------------------------------------------
# Run one schedule
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    """Recursive JSON-safe coercion for a value NESTED inside a dict/list
    (see `_distill_artifact` for the top-level rule, which DROPS an
    unrecognized value instead of coercing it to `None`). `Decimal` becomes
    `str(value)` — never `float` — so a bigquery_sql row's exact decimal
    value round-trips instead of picking up float rounding error."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytes):
        return {"bytes": len(value)}
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return None  # a live object nested inside a dict/list — safe null, never raises


def _distill_artifact(artifact: dict) -> dict:
    """A JSON-safe subset of one step's artifact, for `jobs.result_summary`.
    registry.py's own docstring assigns this to the run loop, not the
    registry: an artifact may hold live objects (a `Report` ORM row, raw PDF/
    Excel bytes) that never belong in a persisted JSON column, OR — a
    `bigquery_sql` artifact's row values — `Decimal`/`date`/`datetime`
    Python objects that a plain `dict`/`list` pass-through does NOT make
    JSON-safe (review finding, MAJOR): `job.result_summary = {...outputs...}`
    raised `TypeError` at flush time in `_finalize_run`, outside `_run_steps`'
    own protection entirely, for ANY step whose artifact carried one.

    A top-level unrecognized value (e.g. a live ORM row) is still DROPPED,
    exactly as before; a `dict`/`list` value recurses through `_json_safe`,
    which coerces `Decimal`/`date`/`datetime`/`uuid.UUID` and turns an
    unrecognized value NESTED inside it into `None` (it can't be dropped
    without breaking the container's shape). `json.loads(json.dumps(...))` at
    the end is the final belt-and-suspenders guarantee: this function itself
    can never hand back something the `jobs.result_summary` JSON column
    rejects."""
    distilled: dict[str, Any] = {}
    for key, value in artifact.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            distilled[key] = value
        elif isinstance(value, Decimal):
            distilled[key] = str(value)
        elif isinstance(value, (datetime, date)):
            distilled[key] = value.isoformat()
        elif isinstance(value, uuid.UUID):
            distilled[key] = str(value)
        elif isinstance(value, bytes):
            distilled[key] = {"bytes": len(value)}
        elif isinstance(value, dict):
            distilled[key] = _json_safe(value)
        elif isinstance(value, (list, tuple)):
            distilled[key] = _json_safe(list(value))
        # anything else (e.g. a live ORM row) is dropped at the TOP LEVEL —
        # it is still available in-memory via ctx.artifacts for a later step.
    return json.loads(json.dumps(distilled))


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
    usage = {"bytes_scanned": 0, "seconds": 0.0, "usd": 0.0}
    started_at = time.monotonic()

    for step in steps:
        # Tenant context unconditionally, EVERY step — read and write alike
        # (review finding, MAJOR). The caller commits right before the loop
        # starts (the `jobs.run.start` audit commit), which clears the
        # transaction-scoped `SET LOCAL app.current_tenant_id`; a READ step
        # used to get no context reset at all, so a plan whose first (or
        # only) step is a read — e.g. `recon.run`, whose own callee
        # (`OrderReconJob.run`) does not self-manage context for its initial
        # queries either — ran with app.current_tenant_id unset. Re-setting
        # it fresh at the top of every iteration means a step never depends
        # on what an earlier step (or the caller) happened to leave in scope.
        await set_tenant_context(db, str(tenant_id))

        step_id = step.get("id")
        step_type = step.get("type")
        spec = STEP_REGISTRY.get(step_type)  # looked up FRESH — see module docstring
        if spec is None:
            return REASON_ERROR, outputs, f"step {step_id!r}: unknown step type {step_type!r} — not in the registry"

        params = step.get("params") or {}

        if spec.kind == "write":
            idem_key = spec.idempotency(ctx, params)
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
            # A DeliveryUnavailable-raising executor may still have touched
            # the DB before raising (e.g. a partial write attempt) -- roll
            # back unconditionally so the caller's post-loop bookkeeping
            # (re-fetching Schedule/Job, retry-then-pause, the final commit)
            # always runs against a clean transaction. A rollback on an
            # already-clean transaction is a harmless no-op.
            await db.rollback()
            return REASON_BLOCKED, outputs, str(exc)
        except StepExecutionError as exc:
            await db.rollback()
            return REASON_ERROR, outputs, str(exc)
        except Exception as exc:  # an executor's own unexpected failure
            logger.exception("scheduled_jobs.step_failed", extra={"step_id": step_id, "step_type": step_type})
            # A genuine DB-level error (SQLAlchemy/asyncpg) leaves the
            # session's transaction aborted; without this rollback,
            # run_schedule_now's very next statement (set_tenant_context)
            # raises InFailedSqlTransactionError and the schedule's
            # retry/pause bookkeeping never runs (review finding).
            await db.rollback()
            return REASON_ERROR, outputs, f"{type(exc).__name__}: {exc}"

        ctx.artifacts[step_id] = artifact
        outputs[step_id] = _distill_artifact(artifact)

        usage["bytes_scanned"] += int(artifact.get("bytes_processed") or 0)
        usage["seconds"] = time.monotonic() - started_at
        usage["usd"] = usage["bytes_scanned"] * _BIGQUERY_USD_PER_BYTE

        limit_bytes = budget.get("bytes_scanned")
        limit_seconds = budget.get("seconds")
        limit_usd = budget.get("usd")
        over_budget = (
            (limit_bytes is not None and usage["bytes_scanned"] > limit_bytes)
            or (limit_seconds is not None and usage["seconds"] > limit_seconds)
            or (limit_usd is not None and usage["usd"] > limit_usd)
        )
        if over_budget:
            return REASON_BUDGET, outputs, None

    return REASON_DONE, outputs, None


async def _finalize_run(
    db: AsyncSession,
    *,
    schedule_id: uuid.UUID,
    tenant_id: uuid.UUID,
    job_id_value: uuid.UUID,
    reason: str,
    outputs: dict[str, Any],
    detail: str | None,
    now: datetime,
) -> tuple[Schedule, Job]:
    """Re-fetch the `Schedule` + `Job` rows fresh and stamp this run's outcome
    onto both. Split out of `run_schedule_now` so a crash HERE (review
    finding: this layer sits outside `_run_steps`'s own rollback-before-return
    protection — a stale row, a connectivity blip, anything) can be retried
    once against a freshly rolled-back session by the caller, instead of
    leaving the `jobs` row it's about to update stuck at status="running"
    forever with no retry-then-pause bookkeeping ever applied."""
    await set_tenant_context(db, str(tenant_id))
    row = (
        await db.execute(select(Schedule).where(Schedule.id == schedule_id, Schedule.tenant_id == tenant_id))
    ).scalar_one()
    job = await db.get(Job, job_id_value)

    job.status = "completed" if reason in (REASON_DONE, REASON_BUDGET, REASON_BLOCKED) else "failed"
    job.completed_at = datetime.now(timezone.utc)
    job.result_summary = {"reason": reason, "outputs": outputs, "detail": detail}
    if detail:
        job.error_message = detail

    row.last_run_at = now
    row.last_run_status = reason
    return row, job


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
    existing_job_id: uuid.UUID | None = None,
    retry_on_error: bool = False,
) -> RunOutcome:
    """Run one schedule ONE time: one `jobs` row, plan steps replayed in order,
    schedule bookkeeping updated, one commit at the end (plus the write-step
    audit-before-call commits inside `_run_steps`).

    `existing_job_id` (Task 5 residual): the request-scoped "Run now" endpoint
    (`POST /schedules/{id}/run`) now enqueues via Celery instead of executing
    inline on the request's own session (a real Inventory Aging run can take
    minutes; nginx cuts the request). That endpoint creates the `jobs` row
    itself — status `pending` — BEFORE dispatching `tasks.scheduled_jobs_run_now`
    (below), so its `202` response can carry the real id immediately; this
    param makes the run below REUSE that SAME row (update in place) instead of
    inserting a second one. `None` (every other caller — the Beat sweep, the
    MCP `schedule.run` tool) keeps the original insert-a-fresh-row behaviour.
    If the given id no longer resolves (defensive only — e.g. the schedule was
    deleted between enqueue and pickup), falls back to inserting fresh rather
    than crashing the run.

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

    `retry_on_error` (review finding, MAJOR) gates the entire retry-then-pause
    block below — ONLY `run_due_jobs` (the sweep) passes `True`. Every other
    caller (the Celery "Run now" path, the MCP `schedule.run` tool) keeps the
    default `False`: a failing manual run must stamp the jobs row + this
    schedule's `last_run_status="error"` (via `_finalize_run`, unconditionally)
    and stop there — it must NOT overwrite `next_run_at` with `now + 15 min`
    (clobbering the real next cron occurrence), set `paused_at`/
    `pause_reason`, or flip `last_run_status` to `retry_pending`/`paused`.
    Before this fix, a `use_pending=True` preview run failing from the API or
    the MCP tool would silently schedule a retry of the schedule's APPROVED
    plan — a plan the operator never asked to run again.

    The retry itself keeps the ORIGINAL occurrence's `period_key` (see the
    lookup below, right before `job_parameters` is built): the retry claim's
    `due_at` is `now + 15 min` from attempt 1, which is the WRONG basis for a
    period key whenever the retry crosses local midnight (review finding).

    HITL gate (review finding, spec Goal line: "approved by a person, run
    deterministically"): `use_pending=False` refuses to run unless
    `plan_status == "approved"` — a non-empty `plan_json` is not proof a
    person approved it (a freshly-compiled schedule already has one, still
    sitting at `plan_status == "pending_approval"`). This is the ONE choke
    point for that rule: every caller (the API's `POST .../run`, the MCP
    `schedule.run` tool, `run_due_jobs`) goes through here, so none of them
    can independently drift out of sync with it. `use_pending=True` is
    deliberately exempt — "Run once with this change" previews a pending,
    not-yet-approved edit on purpose, and `pending_plan_json` only ever
    exists on a schedule whose `plan_json` was approved once already.
    """
    now = now or datetime.now(timezone.utc)
    due_at = due_at or now

    await set_tenant_context(db, str(tenant_id))
    row = (
        await db.execute(select(Schedule).where(Schedule.id == schedule_id, Schedule.tenant_id == tenant_id))
    ).scalar_one()

    # HITL gate (spec Goal line: "approved by a person, run deterministically"):
    # `use_pending=False` replays the schedule's live `plan_json`, which must
    # have gone through `POST .../approve` — `plan_json` being non-empty is
    # NOT the same fact as a person having approved it (a freshly-compiled
    # schedule sits at `plan_status == "pending_approval"` with a real
    # `plan_json` already on it, straight off `POST /schedules`). This check
    # deliberately does NOT apply when `use_pending=True`: "Run once with
    # this change" (spec §B5) is the documented escape hatch to preview an
    # edited-but-not-yet-approved `pending_plan_json` BEFORE approving it,
    # and `pending_plan_json` only ever exists on a schedule whose `plan_json`
    # was already approved once (see `PATCH /schedules/{id}` in schedules.py).
    if not use_pending and row.plan_status != "approved":
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
            payload={"detail": "plan not approved", "plan_status": row.plan_status, "use_pending": use_pending},
            status="error",
        )
        await db.commit()
        return RunOutcome(reason=REASON_BLOCKED, jobs_row_id=None, outputs={})

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

    # The retry (attempt >= 2) must keep attempt 1's OWN period_key, not one
    # computed from the retry's own due_at (review finding, MAJOR): the retry
    # claim's due_at is `now + RETRY_DELAY_MINUTES` from attempt 1 (see the
    # retry branch below), which is the WRONG basis for a period key whenever
    # the retry crosses local midnight -- and different from attempt 1's even
    # when it doesn't. The Drive idempotency key and the recon.run window are
    # both keyed on period_key, so a wrong one here silently targets the
    # WRONG day's period on the retry. Falls back to the computed value only
    # when no attempt-1 row exists (defensive — e.g. it was purged).
    retry_of_job_id: uuid.UUID | None = None
    if attempt >= 2:
        attempt1_stmt = (
            select(Job)
            .where(
                Job.tenant_id == tenant_id,
                Job.job_type == "scheduled_job",
                Job.parameters["schedule_id"].astext == str(schedule_id),
                Job.parameters["attempt"].astext == "1",
            )
            .order_by(Job.started_at.desc())
            .limit(1)
        )
        attempt1_job = (await db.execute(attempt1_stmt)).scalars().first()
        if attempt1_job is not None and attempt1_job.parameters:
            original_period_key = attempt1_job.parameters.get("period_key")
            if original_period_key:
                period_key = original_period_key
            retry_of_job_id = attempt1_job.id

    job_parameters = {
        "schedule_id": str(schedule_id),
        "plan_version": plan_version_used,
        "period_key": period_key,
        "attempt": attempt,
        "use_pending": use_pending,
        "retry_of_job_id": str(retry_of_job_id) if retry_of_job_id else None,
    }

    job: Job | None = None
    if existing_job_id is not None:
        job = await db.get(Job, existing_job_id)
    if job is not None:
        job.status = "running"
        job.correlation_id = correlation_id
        job.started_at = now
        job.parameters = job_parameters
    else:
        job = Job(
            tenant_id=tenant_id,
            job_type="scheduled_job",
            status="running",
            correlation_id=correlation_id,
            started_at=now,
            parameters=job_parameters,
        )
        db.add(job)
    await db.flush()
    # Captured now (job is freshly flushed, not expired) rather than read off
    # `job.id` after `_run_steps` returns: a rollback inside `_run_steps`
    # (review finding) expires every attribute on `job`, including its PK,
    # and re-reading an expired attribute outside an awaited DB call raises
    # sqlalchemy.exc.MissingGreenlet.
    job_id_value = job.id

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

    try:
        row, job = await _finalize_run(
            db,
            schedule_id=schedule_id,
            tenant_id=tenant_id,
            job_id_value=job_id_value,
            reason=reason,
            outputs=outputs,
            detail=detail,
            now=now,
        )
    except Exception as exc:
        # This layer sits OUTSIDE `_run_steps`'s own rollback-before-return
        # protection: a crash re-fetching Schedule/Job (or in the stamping
        # above) must still trigger retry-then-pause (spec §B4), or the
        # `jobs` row created above is left stuck at status="running" forever
        # (review finding — reachable even when every step itself succeeded).
        # Roll back and retry ONCE against a clean session, forcing
        # reason=error so the retry/pause branch below always fires,
        # regardless of what `_run_steps` actually returned.
        logger.exception(
            "scheduled_jobs.run_schedule_now.finalize_failed",
            extra={"schedule_id": str(schedule_id), "job_id": str(job_id_value)},
        )
        try:
            await db.rollback()
        except Exception:
            logger.warning("scheduled_jobs.run_schedule_now.rollback_failed", exc_info=True)
        reason, detail = REASON_ERROR, f"{type(exc).__name__}: {exc}"
        try:
            row, job = await _finalize_run(
                db,
                schedule_id=schedule_id,
                tenant_id=tenant_id,
                job_id_value=job_id_value,
                reason=reason,
                outputs=outputs,
                detail=detail,
                now=now,
            )
        except Exception as exc2:
            # The ONE retry ITSELF failed too (review finding, MINOR): the
            # ORM re-fetch path is apparently broken, not just flaky once —
            # do NOT try it a third time. Fall back to a MINIMAL raw SQL
            # UPDATE by id on both rows, with no ORM re-fetch (that is
            # exactly what just failed twice), so the `jobs` row is never
            # left stuck at status="running" forever with `next_run_at`
            # already advanced by the claim and no record of what happened.
            # This intentionally skips the normal retry-then-pause bookkeeping
            # below (it needs the `row`/`job` ORM objects this branch does not
            # have) — a schedule reaching this branch needs a human to look at
            # it, not a clever third automatic attempt.
            logger.exception(
                "scheduled_jobs.run_schedule_now.finalize_retry_failed",
                extra={"schedule_id": str(schedule_id), "job_id": str(job_id_value)},
            )
            try:
                await db.rollback()
            except Exception:
                logger.warning("scheduled_jobs.run_schedule_now.finalize_retry_rollback_failed", exc_info=True)

            fallback_detail = f"{type(exc2).__name__}: {exc2}"
            fallback_now = datetime.now(timezone.utc)
            fallback_summary = json.dumps({"reason": REASON_ERROR, "outputs": outputs, "detail": fallback_detail})
            try:
                await db.execute(
                    text(
                        "UPDATE jobs SET status = 'failed', "
                        "result_summary = CAST(:result_summary AS JSON), "
                        "error_message = :error_message, "
                        "completed_at = :completed_at "
                        "WHERE id = :job_id"
                    ),
                    {
                        "result_summary": fallback_summary,
                        "error_message": fallback_detail[:1000],
                        "completed_at": fallback_now,
                        "job_id": job_id_value,
                    },
                )
                await db.execute(
                    text(
                        "UPDATE schedules SET last_run_status = 'error', "
                        "last_run_at = :last_run_at WHERE id = :schedule_id"
                    ),
                    {"last_run_at": fallback_now, "schedule_id": schedule_id},
                )
                await db.commit()
            except Exception:
                logger.exception(
                    "scheduled_jobs.run_schedule_now.finalize_fallback_update_failed",
                    extra={"schedule_id": str(schedule_id), "job_id": str(job_id_value)},
                )
                try:
                    await db.rollback()
                except Exception:
                    logger.warning("scheduled_jobs.run_schedule_now.finalize_fallback_rollback_failed", exc_info=True)

            try:
                await audit_service.log_event(
                    db,
                    tenant_id=tenant_id,
                    category="jobs",
                    action="jobs.run.finalize_failed",
                    actor_id=None,
                    actor_type="system",
                    resource_type="job",
                    resource_id=str(job_id_value),
                    correlation_id=correlation_id,
                    job_id=job_id_value,
                    payload={"schedule_id": str(schedule_id), "detail": fallback_detail},
                    status="error",
                )
                await db.commit()
            except Exception:
                logger.exception(
                    "scheduled_jobs.run_schedule_now.finalize_failure_audit_failed",
                    extra={"schedule_id": str(schedule_id), "job_id": str(job_id_value)},
                )
                try:
                    await db.rollback()
                except Exception:
                    logger.warning(
                        "scheduled_jobs.run_schedule_now.finalize_failure_audit_rollback_failed", exc_info=True
                    )

            return RunOutcome(reason=REASON_ERROR, jobs_row_id=job_id_value, outputs=outputs)

    if reason == REASON_ERROR and retry_on_error:
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
    per-item try/except + rollback pattern).

    `retry_on_error=True` on every call below: this IS the sweep, the one
    caller allowed to schedule the 15-minutes-later retry-then-pause cycle
    (see `run_schedule_now`'s own docstring). `existing_job_id=claim.job_id`
    reuses the `jobs` row `_claim_due_schedules` already created, in the same
    transaction as the claim itself, for every runnable claim."""
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
                retry_on_error=True,
                existing_job_id=claim.job_id,
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


@celery_app.task(base=InstrumentedTask, name="tasks.scheduled_jobs_run_now", queue="sync")
def run_schedule_now_task(schedule_id: str, tenant_id: str, use_pending: bool, actor_id: str | None, job_id: str):
    """Celery wrapper for a request-scoped "Run now" (Task 5 residual, spec
    §B5): `POST /schedules/{id}/run` used to call `run_schedule_now` directly
    on the REQUEST's own session, holding the HTTP request open for as long
    as the plan took to run — a real Inventory Aging run takes minutes, which
    nginx cuts. The endpoint now creates the `jobs` row itself (status
    `pending`, so its `202` response can carry the real id immediately) and
    dispatches this task with that row's id; `run_schedule_now`'s
    `existing_job_id` (see its own docstring) makes it reuse that SAME row
    rather than inserting a second one. Own session — never the request's —
    same convention as every other Celery entry point in this module."""
    import asyncio
    import uuid as _uuid

    from app.core.database import set_tenant_context_session, worker_async_session

    async def _run() -> dict:
        async with worker_async_session() as db:
            await set_tenant_context_session(db, tenant_id)
            outcome = await run_schedule_now(
                db,
                _uuid.UUID(schedule_id),
                tenant_id=_uuid.UUID(tenant_id),
                actor_id=_uuid.UUID(actor_id) if actor_id else None,
                actor_type="user",
                use_pending=use_pending,
                existing_job_id=_uuid.UUID(job_id),
            )
            return {
                "reason": outcome.reason,
                "jobs_id": str(outcome.jobs_row_id) if outcome.jobs_row_id else None,
            }

    return asyncio.run(_run())
