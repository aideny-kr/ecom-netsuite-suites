"""Executor + Beat sweep (Slice 2, Task 3). Spec §B4 (binding):

    Beat entry `scheduled-jobs-sweep` every minute -> `scheduled_jobs_sweep_all`
    -> `run_due_jobs(tenant_id)`: `SELECT ... FOR UPDATE SKIP LOCKED` on due
    schedules; catch-up = run once if a run was missed; compute `next_run_at`
    with `croniter` in the schedule's timezone. Each run = one `jobs` row via
    the instrumented task base; steps execute in order with the run's budget
    enforced between steps; the run ends with a reason enum
    (`done|budget|stall|error|blocked`); on `error`: schedule one retry 15 min
    later, then pause + notify the owner. WRITE steps audit `started` with the
    idempotency key before the call.

Every scenario below drives `run_due_jobs`/`run_schedule_now` directly against
the real local Postgres test DB with FAKE step executors monkeypatched onto
`STEP_REGISTRY` — the run loop looks each step's type up in `STEP_REGISTRY` at
RUN time (never caches it), so a monkeypatched entry is picked up exactly like
a real one, with no BigQuery/Drive/WeasyPrint credentials involved.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.core.database import set_tenant_context
from app.models.audit import AuditEvent
from app.models.feature_flag import TenantFeatureFlag
from app.models.job import Job
from app.models.pipeline import Schedule
from app.models.tenant import Tenant, TenantConfig
from app.services.jobs.registry import STEP_REGISTRY, StepExecutionError, StepSpec
from app.workers.tasks import scheduled_jobs
from app.workers.tasks.scheduled_jobs import (
    REASON_BLOCKED,
    REASON_BUDGET,
    REASON_DONE,
    REASON_ERROR,
    RunOutcome,
    _distill_artifact,
    compute_next_run,
    run_due_jobs,
    run_schedule_now,
)
from tests.conftest import create_test_tenant


def _fake_spec(kind: str, executor, idempotency=None, step_type: str = "fake.step") -> StepSpec:
    return StepSpec(
        type=step_type,
        label="Fake step (test)",
        kind=kind,
        params_schema={"type": "object"},
        executor=executor,
        idempotency=idempotency,
    )


async def _seed_job_schedule(
    db: AsyncSession,
    tenant: Tenant,
    *,
    plan_json: dict | None,
    next_run_at: datetime | None,
    cron_expression: str = "0 6 * * 1",
    tz: str = "UTC",
    budget_json: dict | None = None,
    catch_up: str = "once",
    pending_plan_json: dict | None = None,
    plan_version: int = 1,
    plan_status: str = "approved",
    paused_at: datetime | None = None,
    last_run_status: str | None = None,
) -> Schedule:
    schedule = Schedule(
        tenant_id=tenant.id,
        name="Inventory Aging Weekly (test)",
        schedule_type="job",
        cron_expression=cron_expression,
        timezone=tz,
        is_active=True,
        instruction="every Monday, deliver the inventory aging report",
        plan_json=plan_json,
        plan_version=plan_version,
        plan_status=plan_status,
        pending_plan_json=pending_plan_json,
        budget_json=budget_json,
        catch_up=catch_up,
        next_run_at=next_run_at,
        paused_at=paused_at,
        last_run_status=last_run_status,
    )
    db.add(schedule)
    await db.flush()
    return schedule


# ---------------------------------------------------------------------------
# Due detection in the schedule's own timezone
# ---------------------------------------------------------------------------


def test_compute_next_run_is_dst_correct_weekly_pt_september():
    # Monday 06:00 America/Los_Angeles is 13:00 UTC in September (PDT, UTC-7).
    after = datetime(2026, 9, 1, tzinfo=timezone.utc)  # Tuesday
    nxt = compute_next_run("0 6 * * 1", "America/Los_Angeles", after)
    assert nxt == datetime(2026, 9, 7, 13, 0, tzinfo=timezone.utc)


def test_compute_next_run_is_dst_correct_weekly_pt_january():
    # Same cron, same tz — 14:00 UTC in January (PST, UTC-8). Proves the DST
    # offset is recomputed per fire time, not baked in once.
    after = datetime(2026, 1, 1, tzinfo=timezone.utc)
    nxt = compute_next_run("0 6 * * 1", "America/Los_Angeles", after)
    assert nxt == datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)


def test_compute_next_run_is_always_strictly_after_after():
    after = datetime(2026, 9, 7, 13, 0, tzinfo=timezone.utc)  # exactly a fire time
    nxt = compute_next_run("0 6 * * 1", "America/Los_Angeles", after)
    assert nxt > after


# ---------------------------------------------------------------------------
# A compute_next_run failure must pause the schedule, never silently `continue`
# (review finding, MAJOR): a bad cron_expression/timezone used to hit the
# bare `except Exception: ... continue` branch, leaving `next_run_at <= now`
# forever -- the sweep re-claimed the same row every minute, forever, with no
# record anywhere that anything was wrong.
# ---------------------------------------------------------------------------


async def test_claim_pauses_a_schedule_whose_next_run_at_cannot_be_computed(db: AsyncSession, monkeypatch):
    tenant = await create_test_tenant(db, name="Bad Cron Co")
    await set_tenant_context(db, str(tenant.id))

    async def fake_exec(ctx, params):
        return {"ok": True}

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", fake_exec))

    now = datetime.now(timezone.utc)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=now - timedelta(minutes=1),
        # Written directly to the row -- the schema validators reject this at
        # the API/compiler layer, but nothing stops a row already in the
        # table (or a future validator gap) from carrying one.
        cron_expression="not a cron",
    )
    schedule_id = schedule.id

    stats = await run_due_jobs(db, tenant.id, now=now)
    assert stats["due"] == 0  # never claimed as runnable -- paused, not run
    assert stats["ran"] == 0

    refreshed = (await db.execute(select(Schedule).where(Schedule.id == schedule_id))).scalar_one()
    assert refreshed.paused_at is not None
    assert refreshed.pause_reason is not None
    assert refreshed.pause_reason.startswith("paused: schedule cannot be computed")
    assert refreshed.last_run_status == "paused"
    assert refreshed.next_run_at is None

    pause_events = (
        (
            await db.execute(
                select(AuditEvent).where(AuditEvent.tenant_id == tenant.id, AuditEvent.action == "jobs.paused")
            )
        )
        .scalars()
        .all()
    )
    assert len(pause_events) == 1
    assert pause_events[0].payload["reason"] == refreshed.pause_reason
    assert pause_events[0].status == "error"

    # No jobs row -- this schedule never ran.
    jobs = (await db.execute(select(Job).where(Job.tenant_id == tenant.id))).scalars().all()
    assert jobs == []

    # A second sweep must not re-claim the now-paused row.
    now2 = now + timedelta(minutes=5)
    stats2 = await run_due_jobs(db, tenant.id, now=now2)
    assert stats2["due"] == 0


# ---------------------------------------------------------------------------
# The claim creates the `jobs` row INSIDE the claim transaction (review
# finding, MAJOR): `_claim_due_schedules` used to commit `next_run_at`
# advanced + `running` before any `jobs` row existed -- a worker crash
# between that commit and `run_schedule_now`'s own insert dropped the
# occurrence with NO record at all, anywhere.
# ---------------------------------------------------------------------------


async def test_claim_creates_the_jobs_row_before_run_schedule_now_executes_any_step(db: AsyncSession, monkeypatch):
    tenant = await create_test_tenant(db, name="Claim Job Row Co")
    await set_tenant_context(db, str(tenant.id))

    async def fake_exec(ctx, params):
        return {"ok": True}

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", fake_exec))

    now = datetime.now(timezone.utc)
    await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=now - timedelta(minutes=1),
        cron_expression="0 6 * * 1",
    )

    seen_before_run: list[dict] = []
    real_run_schedule_now = scheduled_jobs.run_schedule_now

    async def spying_run_schedule_now(db_, schedule_id, **kwargs):
        existing_job_id = kwargs.get("existing_job_id")
        rows = (await db_.execute(select(Job).where(Job.tenant_id == tenant.id))).scalars().all()
        seen_before_run.append(
            {
                "existing_job_id": existing_job_id,
                "row_ids": [r.id for r in rows],
                "statuses": [r.status for r in rows],
                "parameters": [r.parameters for r in rows],
            }
        )
        return await real_run_schedule_now(db_, schedule_id, **kwargs)

    monkeypatch.setattr(scheduled_jobs, "run_schedule_now", spying_run_schedule_now)

    stats = await run_due_jobs(db, tenant.id, now=now)
    assert stats["ran"] == 1

    # Exactly one call, and by the time it happened, a jobs row ALREADY
    # existed (created + committed during the claim, before any step ran).
    assert len(seen_before_run) == 1
    snap = seen_before_run[0]
    assert snap["existing_job_id"] is not None
    assert snap["row_ids"] == [snap["existing_job_id"]]
    assert snap["statuses"] == ["pending"]
    assert snap["parameters"][0]["schedule_id"]
    assert snap["parameters"][0]["attempt"] == 1
    assert "due_at" in snap["parameters"][0]

    # No second row -- run_schedule_now must reuse the same one.
    jobs = (await db.execute(select(Job).where(Job.tenant_id == tenant.id))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].id == snap["existing_job_id"]
    assert jobs[0].status == "completed"


# ---------------------------------------------------------------------------
# FOR UPDATE SKIP LOCKED prevents a double run
# ---------------------------------------------------------------------------


async def test_skip_locked_prevents_double_run(monkeypatch):
    """Two GENUINELY CONCURRENT sweeps (separate engines/connections, real
    commits) on the same due schedule -> exactly one claim succeeds and
    exactly one `jobs` row is created. Bypasses the rollback `db` fixture —
    SKIP LOCKED's guarantee is a cross-TRANSACTION one; a single shared
    connection cannot exercise it."""
    db_url = settings.DATABASE_URL_DIRECT or settings.DATABASE_URL
    if "supabase" in db_url:
        pytest.skip("committing concurrency test runs against LOCAL docker only")

    engine = create_async_engine(db_url, echo=False)
    tenant_id: uuid.UUID | None = None
    try:
        async with AsyncSession(engine, expire_on_commit=False) as seed:
            tenant = await create_test_tenant(seed, name="SkipLocked Co", slug=f"skiplocked-{uuid.uuid4().hex[:8]}")
            tenant_id = tenant.id
            now = datetime.now(timezone.utc)
            await _seed_job_schedule(
                seed,
                tenant,
                plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
                next_run_at=now - timedelta(minutes=1),
                cron_expression="* * * * *",
            )
            await seed.commit()

        started = asyncio.Event()
        proceed = asyncio.Event()
        call_count = {"n": 0}

        async def hook():
            call_count["n"] += 1
            if call_count["n"] == 1:
                started.set()
                await asyncio.wait_for(proceed.wait(), timeout=10)

        monkeypatch.setattr(scheduled_jobs, "_claim_sync_hook", hook)

        async def fake_step_exec(ctx, params):
            return {"ok": True}

        monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", fake_step_exec))

        results: dict[str, dict] = {}

        async def run_a():
            async with AsyncSession(engine, expire_on_commit=False) as db_a:
                results["a"] = await run_due_jobs(db_a, tenant_id, now=now)

        async def run_b():
            await asyncio.wait_for(started.wait(), timeout=10)
            async with AsyncSession(engine, expire_on_commit=False) as db_b:
                results["b"] = await run_due_jobs(db_b, tenant_id, now=now)
            proceed.set()

        await asyncio.gather(run_a(), run_b())

        assert results["b"]["due"] == 0  # skipped — the row was locked by A
        assert results["a"]["due"] == 1
        assert results["a"]["ran"] == 1

        async with AsyncSession(engine, expire_on_commit=False) as check:
            jobs = (await check.execute(select(Job).where(Job.tenant_id == tenant_id))).scalars().all()
            assert len(jobs) == 1, f"expected exactly one jobs row from two concurrent sweeps, got {len(jobs)}"
    finally:
        if tenant_id is not None:
            async with AsyncSession(engine, expire_on_commit=False) as cleanup:
                await cleanup.execute(Job.__table__.delete().where(Job.tenant_id == tenant_id))
                await cleanup.execute(AuditEvent.__table__.delete().where(AuditEvent.tenant_id == tenant_id))
                await cleanup.execute(Schedule.__table__.delete().where(Schedule.tenant_id == tenant_id))
                await cleanup.execute(
                    TenantFeatureFlag.__table__.delete().where(TenantFeatureFlag.tenant_id == tenant_id)
                )
                await cleanup.execute(TenantConfig.__table__.delete().where(TenantConfig.tenant_id == tenant_id))
                await cleanup.execute(Tenant.__table__.delete().where(Tenant.id == tenant_id))
                await cleanup.commit()
        await engine.dispose()


# ---------------------------------------------------------------------------
# Catch-up runs once, next_run_at advances past now
# ---------------------------------------------------------------------------


async def test_catch_up_runs_once_and_advances_next_run_at_past_now(db: AsyncSession, monkeypatch):
    tenant = await create_test_tenant(db, name="CatchUp Co")
    await set_tenant_context(db, str(tenant.id))

    calls: list[dict] = []

    async def fake_exec(ctx, params):
        calls.append(dict(params))
        return {"ok": True}

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", fake_exec))

    now = datetime.now(timezone.utc)
    missed_due = now - timedelta(days=10)  # weekly cron: ten days overdue == missed at least one window
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {"x": 1}}]},
        next_run_at=missed_due,
        cron_expression="0 6 * * 1",
        catch_up="once",
    )

    stats = await run_due_jobs(db, tenant.id, now=now)

    assert stats["due"] == 1
    assert stats["ran"] == 1
    assert len(calls) == 1  # ran exactly once — not once per missed week

    assert schedule.next_run_at > now
    assert schedule.last_run_status == REASON_DONE

    jobs = (await db.execute(select(Job).where(Job.tenant_id == tenant.id))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].job_type == "scheduled_job"
    assert jobs[0].result_summary["reason"] == REASON_DONE


async def test_catch_up_skip_never_skips_the_pending_retry(db: AsyncSession, monkeypatch):
    """review finding, MAJOR: `skip = row.catch_up == "skip" and missed` also
    fired for the attempt-2 retry, marking it `skipped` with `run=False` --
    retry-then-pause never actually ran the retry, silently swallowing the
    original failure forever. `skip` must only ever apply to a fresh
    attempt-1 claim. Item 1 (gate fix): attempt is read off the explicit
    `retry_job_id` column now, so the pending retry needs a REAL pre-created
    `jobs` row to point at (created eagerly at scheduling time, never at
    claim time -- see `run_schedule_now`'s retry-then-pause branch)."""
    tenant = await create_test_tenant(db, name="CatchUpSkip Retry Co")
    await set_tenant_context(db, str(tenant.id))

    calls: list[dict] = []

    async def fake_exec(ctx, params):
        calls.append(dict(params))
        return {"ok": True}

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", fake_exec))

    now = datetime.now(timezone.utc)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=now - timedelta(minutes=5),
        cron_expression="* * * * *",  # every minute -- 5 minutes overdue == missed
        catch_up="skip",
        last_run_status="retry_pending",
    )
    retry_job = Job(
        tenant_id=tenant.id,
        job_type="scheduled_job",
        status="pending",
        parameters={
            "schedule_id": str(schedule.id),
            "attempt": 2,
            "period_key": "2026-01-01",
            "retry_of_job_id": str(uuid.uuid4()),
            "due_at": (now - timedelta(minutes=5)).isoformat(),
        },
    )
    db.add(retry_job)
    await db.flush()
    schedule.retry_job_id = retry_job.id
    await db.commit()

    stats = await run_due_jobs(db, tenant.id, now=now)

    assert stats["due"] == 1
    assert stats["ran"] == 1  # NOT skipped, even though catch_up="skip" and missed
    assert stats["skipped"] == 0
    assert len(calls) == 1

    jobs = (await db.execute(select(Job).where(Job.tenant_id == tenant.id))).scalars().all()
    assert len(jobs) == 1  # the pre-created retry row is REUSED, not a second one created
    assert jobs[0].id == retry_job.id
    assert jobs[0].parameters["attempt"] == 2
    assert schedule.last_run_status == REASON_DONE
    assert schedule.retry_job_id is None  # cleared at claim time


async def test_catch_up_skip_does_not_run_a_missed_window(db: AsyncSession, monkeypatch):
    tenant = await create_test_tenant(db, name="CatchUpSkip Co")
    await set_tenant_context(db, str(tenant.id))

    calls: list[dict] = []

    async def fake_exec(ctx, params):
        calls.append(dict(params))
        return {"ok": True}

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", fake_exec))

    now = datetime.now(timezone.utc)
    missed_due = now - timedelta(days=10)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=missed_due,
        cron_expression="0 6 * * 1",
        catch_up="skip",
    )

    stats = await run_due_jobs(db, tenant.id, now=now)

    assert stats["due"] == 1
    assert stats["ran"] == 0
    assert stats["skipped"] == 1
    assert len(calls) == 0
    assert schedule.next_run_at > now

    jobs = (await db.execute(select(Job).where(Job.tenant_id == tenant.id))).scalars().all()
    assert len(jobs) == 0


# ---------------------------------------------------------------------------
# Step error -> retry once after 15 min -> pause + owner-notification audit
# ---------------------------------------------------------------------------


async def test_step_error_retries_once_then_pauses(db: AsyncSession, monkeypatch):
    tenant = await create_test_tenant(db, name="Retry Co")
    # Captured now (fix for the MAJOR tenant-context finding above): a step
    # that raises with NO db access of its own used to make `_run_steps`'s
    # `db.rollback()` a no-op (nothing had opened a transaction since the
    # last commit) -- now that EVERY step unconditionally re-sets tenant
    # context first, that SET LOCAL opens a real transaction, so the
    # subsequent rollback is a real one and expires the whole identity map.
    # `_finalize_run`'s own re-fetch keeps `schedule`/`job` fresh afterward,
    # but nothing re-selects `Tenant` -- re-reading its expired `.id`
    # attribute outside an awaited DB call raises MissingGreenlet, same
    # failure mode `test_db_error_inside_a_step_still_completes_retry_bookkeeping`
    # already documents above.
    tenant_id = tenant.id
    await set_tenant_context(db, str(tenant_id))

    async def always_fails(ctx, params):
        raise StepExecutionError("boom: the fake step always fails")

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", always_fails))

    now = datetime.now(timezone.utc)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=now - timedelta(minutes=1),
        cron_expression="0 6 * * 1",
    )

    # Attempt 1: fails -> reason=error, retry scheduled 15 minutes out, not paused yet.
    stats1 = await run_due_jobs(db, tenant_id, now=now)
    assert stats1["ran"] == 1
    assert stats1["failed"] == 1
    assert schedule.paused_at is None
    assert schedule.last_run_status == "retry_pending"
    retry_at = schedule.next_run_at
    assert retry_at is not None
    assert retry_at - now >= timedelta(minutes=14)  # ~15 minutes, allow test-clock slack
    assert retry_at - now <= timedelta(minutes=16)

    # Item 1 (gate fix): the retry's `jobs` row is created EAGERLY, right here
    # at scheduling time (not lazily at the second sweep's claim) -- so a
    # second, still-pending attempt-2 row already exists after just attempt 1.
    jobs_after_1 = (await db.execute(select(Job).where(Job.tenant_id == tenant_id))).scalars().all()
    assert len(jobs_after_1) == 2
    attempt1_job = next(j for j in jobs_after_1 if j.parameters["attempt"] == 1)
    pending_retry_job = next(j for j in jobs_after_1 if j.parameters["attempt"] == 2)
    assert attempt1_job.result_summary["reason"] == REASON_ERROR
    assert pending_retry_job.status == "pending"
    assert pending_retry_job.parameters["retry_of_job_id"] == str(attempt1_job.id)
    assert schedule.retry_job_id == pending_retry_job.id

    # Attempt 2, 15 minutes later: fails again -> pause + pause_reason + owner-notify audit.
    now2 = retry_at + timedelta(seconds=1)
    stats2 = await run_due_jobs(db, tenant_id, now=now2)
    assert stats2["ran"] == 1
    assert schedule.paused_at is not None
    assert schedule.pause_reason
    assert schedule.last_run_status == "paused"

    jobs_after_2 = (await db.execute(select(Job).where(Job.tenant_id == tenant_id))).scalars().all()
    assert len(jobs_after_2) == 2  # still just the two rows -- the retry REUSED the pre-created one
    retry_job = next(j for j in jobs_after_2 if j.parameters.get("attempt") == 2)
    assert retry_job.id == pending_retry_job.id
    assert retry_job.result_summary["reason"] == REASON_ERROR

    pause_events = (
        (
            await db.execute(
                select(AuditEvent).where(AuditEvent.tenant_id == tenant_id, AuditEvent.action == "jobs.paused")
            )
        )
        .scalars()
        .all()
    )
    assert len(pause_events) == 1  # the owner-notification audit event


# ---------------------------------------------------------------------------
# Budget exceeded between steps -> reason=budget, remaining steps never run
# ---------------------------------------------------------------------------


async def test_budget_exceeded_between_steps_stops_remaining_steps(db: AsyncSession, monkeypatch):
    tenant = await create_test_tenant(db, name="Budget Co")
    await set_tenant_context(db, str(tenant.id))

    step2_calls: list[dict] = []

    async def big_step(ctx, params):
        return {"bytes_processed": 10_000_000_000}  # 10 GB — deliberately over the tiny cap below

    async def never_reached(ctx, params):
        step2_calls.append(params)
        return {"ok": True}

    monkeypatch.setitem(STEP_REGISTRY, "fake.big", _fake_spec("read", big_step, step_type="fake.big"))
    monkeypatch.setitem(STEP_REGISTRY, "fake.never", _fake_spec("read", never_reached, step_type="fake.never"))

    now = datetime.now(timezone.utc)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={
            "steps": [
                {"id": "s1", "type": "fake.big", "params": {}},
                {"id": "s2", "type": "fake.never", "params": {}},
            ]
        },
        next_run_at=now - timedelta(minutes=1),
        cron_expression="0 6 * * 1",
        budget_json={"bytes_scanned": 1000},  # 1 KB cap — step 1 alone blows through it
    )

    stats = await run_due_jobs(db, tenant.id, now=now)

    assert stats["ran"] == 1
    assert schedule.last_run_status == REASON_BUDGET
    assert step2_calls == []  # step 2 never ran

    jobs = (await db.execute(select(Job).where(Job.tenant_id == tenant.id))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].result_summary["reason"] == REASON_BUDGET
    assert "s1" in jobs[0].result_summary["outputs"]
    assert "s2" not in jobs[0].result_summary["outputs"]


async def test_usd_only_budget_stops_the_run(db: AsyncSession, monkeypatch):
    """Spec §B4: budget is "(bytes scanned, seconds, usd) enforced between
    steps". A schedule configured with ONLY a usd cap (no bytes_scanned/
    seconds cap at all) must still stop the run (review finding: `usage` only
    tracked bytes_scanned/seconds, so a usd-only cap never fired)."""
    tenant = await create_test_tenant(db, name="USD Budget Co")
    await set_tenant_context(db, str(tenant.id))

    step2_calls: list[dict] = []

    async def pricey_step(ctx, params):
        # 1 TB scanned -> $5 at the existing $5/TB BigQuery rate
        # (app.services.bigquery_service.estimate_query_cost's own constant)
        # -> blows through a $1 cap even though bytes_scanned/seconds are
        # both left unset on the schedule's budget.
        return {"bytes_processed": 1_000_000_000_000}

    async def never_reached(ctx, params):
        step2_calls.append(params)
        return {"ok": True}

    monkeypatch.setitem(STEP_REGISTRY, "fake.pricey", _fake_spec("read", pricey_step, step_type="fake.pricey"))
    monkeypatch.setitem(STEP_REGISTRY, "fake.never", _fake_spec("read", never_reached, step_type="fake.never"))

    now = datetime.now(timezone.utc)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={
            "steps": [
                {"id": "s1", "type": "fake.pricey", "params": {}},
                {"id": "s2", "type": "fake.never", "params": {}},
            ]
        },
        next_run_at=now - timedelta(minutes=1),
        cron_expression="0 6 * * 1",
        budget_json={"usd": 1.0},  # ONLY a usd cap -- no bytes_scanned/seconds cap
    )

    stats = await run_due_jobs(db, tenant.id, now=now)

    assert stats["ran"] == 1
    assert schedule.last_run_status == REASON_BUDGET
    assert step2_calls == []  # step 2 never ran

    jobs = (await db.execute(select(Job).where(Job.tenant_id == tenant.id))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].result_summary["reason"] == REASON_BUDGET
    assert "s1" in jobs[0].result_summary["outputs"]
    assert "s2" not in jobs[0].result_summary["outputs"]


# ---------------------------------------------------------------------------
# _distill_artifact must produce JSON-serializable output, always (review
# finding, MAJOR): it passed dict/list values through unchanged, so a
# bigquery_sql artifact carrying Decimal/date/datetime row values raised
# TypeError at flush time in _finalize_run -- outside _run_steps's own
# protection entirely.
# ---------------------------------------------------------------------------


def test_distill_artifact_coerces_decimal_date_and_drops_unknown_top_level_values():
    class _LiveRow:
        pass

    artifact = {
        "rows": [{"qty": Decimal("1.50"), "as_of": date(2026, 9, 8)}],
        "report": _LiveRow(),
    }
    distilled = _distill_artifact(artifact)
    assert distilled == {"rows": [{"qty": "1.50", "as_of": "2026-09-08"}]}
    # The whole thing must actually round-trip through JSON -- the real
    # guarantee _finalize_run needs when it assigns this to a JSON column.
    import json

    assert json.loads(json.dumps(distilled)) == distilled


async def test_executor_persists_decimal_and_date_artifact_values_without_typeerror(db: AsyncSession, monkeypatch):
    """The end-to-end failure this used to raise: a step returns an artifact
    with Decimal/date values, and `_finalize_run` assigning `job.result_summary`
    (a JSON column) raised TypeError at flush time -- reachable even though
    every step itself "succeeded"."""
    tenant = await create_test_tenant(db, name="Decimal Artifact Co")
    await set_tenant_context(db, str(tenant.id))

    async def bigquery_like_step(ctx, params):
        return {
            "bytes_processed": 100,
            "rows": [{"qty": Decimal("1.50"), "as_of": date(2026, 9, 8)}],
        }

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", bigquery_like_step))

    now = datetime.now(timezone.utc)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=now - timedelta(minutes=1),
        cron_expression="0 6 * * 1",
    )

    stats = await run_due_jobs(db, tenant.id, now=now)
    assert stats["ran"] == 1
    assert stats["failed"] == 0
    assert schedule.last_run_status == REASON_DONE

    jobs = (await db.execute(select(Job).where(Job.tenant_id == tenant.id))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].status == "completed"
    assert jobs[0].result_summary["outputs"]["s1"]["rows"] == [{"qty": "1.50", "as_of": "2026-09-08"}]


# ---------------------------------------------------------------------------
# drive.upload (a WRITE step) audits `started` with the idempotency key
# BEFORE its executor is called.
# ---------------------------------------------------------------------------


async def test_write_step_audits_started_with_idempotency_key_before_the_call(db: AsyncSession, monkeypatch):
    tenant = await create_test_tenant(db, name="Write Audit Co")
    await set_tenant_context(db, str(tenant.id))

    seen_before_call: list[list[AuditEvent]] = []

    def idem(ctx, params) -> str:
        return f"job:{ctx.job_id}:period:{params['period_key']}"

    async def fake_write(ctx, params):
        # The audit-before-call invariant, checked FROM INSIDE the executor —
        # by the time this body runs, the started audit event must already be
        # committed and visible on this same session.
        rows = (
            (
                await ctx.db.execute(
                    select(AuditEvent).where(
                        AuditEvent.tenant_id == tenant.id,
                        AuditEvent.action == "fake.write.started",
                    )
                )
            )
            .scalars()
            .all()
        )
        seen_before_call.append(rows)
        return {"delivered": True}

    monkeypatch.setitem(
        STEP_REGISTRY, "fake.write", _fake_spec("write", fake_write, idempotency=idem, step_type="fake.write")
    )

    now = datetime.now(timezone.utc)
    await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.write", "params": {"period_key": "2026-09-07"}}]},
        next_run_at=now - timedelta(minutes=1),
        cron_expression="0 6 * * 1",
    )

    stats = await run_due_jobs(db, tenant.id, now=now)
    assert stats["ran"] == 1

    assert len(seen_before_call) == 1
    started_events = seen_before_call[0]
    assert len(started_events) == 1
    payload = started_events[0].payload
    assert payload["idempotency_key"].startswith("job:")
    assert payload["idempotency_key"].endswith(":period:2026-09-07")


# ---------------------------------------------------------------------------
# use_pending=True records the pending plan version on the run
# ---------------------------------------------------------------------------


async def test_use_pending_records_the_pending_plan_version_on_the_run(db: AsyncSession, monkeypatch):
    tenant = await create_test_tenant(db, name="Pending Co")
    await set_tenant_context(db, str(tenant.id))

    async def fake_exec(ctx, params):
        return {"ok": True}

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", fake_exec))

    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {"variant": "approved"}}]},
        pending_plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {"variant": "pending"}}]},
        next_run_at=None,
        cron_expression="0 6 * * 1",
        plan_version=3,
    )

    outcome = await run_schedule_now(db, schedule.id, tenant_id=tenant.id, use_pending=True, actor_id=None)

    assert isinstance(outcome, RunOutcome)
    assert outcome.reason == REASON_DONE

    job = (await db.execute(select(Job).where(Job.tenant_id == tenant.id))).scalar_one()
    assert job.parameters["plan_version"] == 4  # the version pending_plan_json WOULD become on approval
    assert job.parameters["use_pending"] is True

    # And it actually replayed the PENDING plan, not the approved one.
    approved_outcome = await run_schedule_now(db, schedule.id, tenant_id=tenant.id, use_pending=False, actor_id=None)
    approved_job = (
        (await db.execute(select(Job).where(Job.tenant_id == tenant.id).order_by(Job.started_at.desc())))
        .scalars()
        .first()
    )
    assert approved_job.parameters["plan_version"] == 3
    assert approved_outcome.reason == REASON_DONE


# ---------------------------------------------------------------------------
# A step that poisons the DB transaction still leaves the run recoverable
# (review finding: _run_steps must roll back before returning on any except
# branch, or run_schedule_now's very next statement -- set_tenant_context --
# raises InFailedSqlTransactionError and the schedule's retry/pause
# bookkeeping never runs, leaking a 'running' jobs row).
# ---------------------------------------------------------------------------


async def test_db_error_inside_a_step_still_completes_retry_bookkeeping(db: AsyncSession, monkeypatch):
    tenant = await create_test_tenant(db, name="Poisoned Txn Co")
    tenant_id = tenant.id  # captured now: a service-side rollback (below) can
    # expire other objects touched earlier in the same session, and re-reading
    # an expired attribute outside an awaited DB call raises MissingGreenlet.
    await set_tenant_context(db, str(tenant_id))

    async def poisons_the_transaction(ctx, params):
        # A real DB-level error (not an app-level StepExecutionError) --
        # Postgres marks the whole transaction aborted, exactly like a
        # genuine asyncpg/SQLAlchemy failure inside a real executor.
        await ctx.db.execute(text("SELECT 1/0"))
        return {"ok": True}  # never reached

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", poisons_the_transaction))

    now = datetime.now(timezone.utc)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=now - timedelta(minutes=1),
        cron_expression="0 6 * * 1",
    )
    schedule_id = schedule.id

    stats = await run_due_jobs(db, tenant_id, now=now)

    # The sweep must not silently swallow this as a bare rollback+log with no
    # bookkeeping -- it must record reason=error and schedule the retry, same
    # as an app-level StepExecutionError would.
    assert stats["ran"] == 1
    assert stats["failed"] == 1

    refreshed_schedule = (await db.execute(select(Schedule).where(Schedule.id == schedule_id))).scalar_one()
    assert refreshed_schedule.last_run_status == "retry_pending"
    assert refreshed_schedule.next_run_at is not None
    assert refreshed_schedule.next_run_at > now

    # Item 1 (gate fix): the retry's `jobs` row is now created EAGERLY, right
    # here at scheduling time -- so the failed attempt-1 row is joined by a
    # second, still-pending attempt-2 row before the retry ever fires.
    job = (await db.execute(select(Job).where(Job.tenant_id == tenant_id, Job.status == "failed"))).scalar_one()
    assert job.result_summary["reason"] == REASON_ERROR


# ---------------------------------------------------------------------------
# A crash in run_schedule_now's OWN post-_run_steps bookkeeping (the
# Schedule/Job re-fetch, outside _run_steps entirely) must still complete
# retry-then-pause (review finding: only _run_steps's own exceptions were
# covered; a crash one layer up leaked a jobs row stuck at status='running'
# with no retry scheduled, reachable even though every step itself succeeded).
# ---------------------------------------------------------------------------


async def test_finalize_crash_after_run_steps_still_completes_retry_bookkeeping(db: AsyncSession, monkeypatch):
    tenant = await create_test_tenant(db, name="Finalize Crash Co")
    tenant_id = tenant.id
    await set_tenant_context(db, str(tenant_id))

    async def ok_step(ctx, params):
        return {"ok": True}

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", ok_step))

    now = datetime.now(timezone.utc)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=now - timedelta(minutes=1),
        cron_expression="0 6 * * 1",
    )
    schedule_id = schedule.id

    real_finalize = scheduled_jobs._finalize_run
    calls = {"n": 0}

    async def flaky_finalize(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulates ANY crash in this layer -- not a DB-transaction
            # poisoning (that's the other test above), a genuine unexpected
            # exception (e.g. a stale row, a connectivity blip) hitting the
            # re-fetch/bookkeeping code that runs AFTER `_run_steps` returns.
            raise RuntimeError("simulated crash re-fetching Schedule/Job")
        return await real_finalize(*args, **kwargs)

    monkeypatch.setattr(scheduled_jobs, "_finalize_run", flaky_finalize)

    stats = await run_due_jobs(db, tenant_id, now=now)

    # The step itself succeeded -- the crash happened only in bookkeeping --
    # but the run must still be recorded as a failure so retry-then-pause
    # fires, not left silently stuck at status="running".
    assert stats["ran"] == 1
    assert stats["failed"] == 1
    assert calls["n"] == 2  # one crash, one successful retry against a clean session

    refreshed_schedule = (await db.execute(select(Schedule).where(Schedule.id == schedule_id))).scalar_one()
    assert refreshed_schedule.last_run_status == "retry_pending"
    assert refreshed_schedule.next_run_at is not None
    assert refreshed_schedule.next_run_at > now

    # Item 1 (gate fix): the retry's `jobs` row is created eagerly at
    # scheduling time, so a second (still-pending) row now exists alongside
    # the failed one.
    job = (await db.execute(select(Job).where(Job.tenant_id == tenant_id, Job.status == "failed"))).scalar_one()
    assert job.result_summary["reason"] == REASON_ERROR


# ---------------------------------------------------------------------------
# Tenant context on EVERY step (review finding, MAJOR): `_run_steps` used to
# call `set_tenant_context` only inside the `if spec.kind == "write":` branch.
# `run_schedule_now` commits right before `_run_steps` even starts (the
# `jobs.run.start` audit commit), which clears the transaction-scoped
# `SET LOCAL app.current_tenant_id` -- so a READ step (or any step that is
# not itself the write branch) ran with no tenant context set at all unless
# something upstream happened to leave it in scope.
#
# The `db` fixture wraps every test in an outer transaction (savepoints,
# `conftest.py`'s own comment above the fixture): a service-side commit here
# is RELEASE SAVEPOINT, which -- unlike a real COMMIT -- does NOT clear
# `SET LOCAL`. Asserting the GUC value directly would therefore FALSE-PASS
# regardless of whether `_run_steps` re-sets it. Following the established
# fix for the identical class of bug (test_report_refresh.py's `_spy_events`
# / "T2-gate round-1 fixes: RLS context across commits" section): spy on
# `set_tenant_context` in the module's own namespace, actually execute the
# real `SET LOCAL` from inside the spy (so the run stays correct), and
# assert on the ORDERING of ctx/commit/exec events instead.
# ---------------------------------------------------------------------------


def _spy_events(monkeypatch, db):
    """Mirrors test_report_refresh.py's `_spy_events` helper for the
    identical class of bug in this module: records `"ctx"`/`"commit"`/
    `"rollback"` events for `scheduled_jobs.set_tenant_context`/`db.commit`/
    `db.rollback`, while still performing the real operation so the run
    itself stays correct."""
    events: list[str] = []
    real_commit, real_rollback = db.commit, db.rollback

    async def spy_ctx(session, tenant_id):
        events.append("ctx")
        await set_tenant_context(session, tenant_id)

    async def spy_commit():
        events.append("commit")
        await real_commit()

    async def spy_rollback():
        events.append("rollback")
        await real_rollback()

    monkeypatch.setattr(scheduled_jobs, "set_tenant_context", spy_ctx)
    monkeypatch.setattr(db, "commit", spy_commit)
    monkeypatch.setattr(db, "rollback", spy_rollback)
    return events


async def test_tenant_context_set_before_every_step_read_and_write(db: AsyncSession, monkeypatch):
    tenant = await create_test_tenant(db, name="Tenant Ctx Order Co")
    events = _spy_events(monkeypatch, db)

    def idem(ctx, params):
        return f"job:{ctx.job_id}:step:{params['label']}"

    async def read_step(ctx, params):
        events.append(f"exec:{params['label']}")
        return {"ok": True}

    async def write_step(ctx, params):
        events.append(f"exec:{params['label']}")
        return {"ok": True}

    monkeypatch.setitem(STEP_REGISTRY, "fake.read", _fake_spec("read", read_step, step_type="fake.read"))
    monkeypatch.setitem(
        STEP_REGISTRY, "fake.write", _fake_spec("write", write_step, idempotency=idem, step_type="fake.write")
    )

    now = datetime.now(timezone.utc)
    await _seed_job_schedule(
        db,
        tenant,
        plan_json={
            "steps": [
                {"id": "s1", "type": "fake.read", "params": {"label": "s1"}},
                {"id": "s2", "type": "fake.write", "params": {"label": "s2"}},
                {"id": "s3", "type": "fake.read", "params": {"label": "s3"}},
            ]
        },
        next_run_at=now - timedelta(minutes=1),
        cron_expression="0 6 * * 1",
    )

    stats = await run_due_jobs(db, tenant.id, now=now)
    assert stats["ran"] == 1
    assert stats["failed"] == 0

    # Every step -- read s1, write s2, read s3 -- must be immediately preceded
    # by a fresh "ctx" event: a READ step is never exempt, and a step is never
    # left to inherit whatever context happened to still be in scope from
    # something earlier (a commit, or nothing at all, both currently show up
    # here as the preceding event before the fix).
    for label in ("s1", "s2", "s3"):
        exec_idx = events.index(f"exec:{label}")
        assert events[exec_idx - 1] == "ctx", (
            f"step {label} must run with tenant context freshly (re-)set immediately before it; "
            f"events around it: {events[max(0, exec_idx - 3) : exec_idx + 1]}"
        )

    # Item 2 (delta gate fix E): every step -- read or write -- must ALSO be
    # immediately FOLLOWED by a commit the moment it succeeds, so a LATER
    # step's failure can never roll back an earlier step's own success (this
    # is the fix under test here: before it, a read step had no commit of
    # its own at all).
    for label in ("s1", "s2", "s3"):
        exec_idx = events.index(f"exec:{label}")
        assert events[exec_idx + 1] == "commit", (
            f"step {label}'s success must be committed immediately, before the next step runs; "
            f"events around it: {events[exec_idx : exec_idx + 3]}"
        )

    # The write step's OWN audit-before-call commit must ALSO sit between two
    # ctx events strictly BEFORE its own exec (top-of-loop ctx, the
    # audit-before-call commit, then re-established ctx) -- this half already
    # worked before the fix; kept here as a regression guard against ever
    # dropping it while adding the post-success commit above. Skip PAST s1's
    # own post-success commit (item 2) to isolate the write step's iteration.
    s1_idx = events.index("exec:s1")
    s2_idx = events.index("exec:s2")
    between = events[s1_idx + 2 : s2_idx]
    assert between == ["ctx", "commit", "ctx"], (
        "expected exactly [ctx, commit, ctx] between s1's post-success commit and s2's own exec "
        f"(top-of-loop ctx, the write step's audit-before-call commit, re-established ctx); got {between}"
    )


async def test_recon_run_as_lone_first_step_runs_with_tenant_context_set(db: AsyncSession, monkeypatch):
    """The same MAJOR finding, exercised through the REAL `recon.run` step
    (not a fake): `OrderReconJob.run` does not self-manage tenant context for
    its own initial queries (it only re-establishes context deep inside,
    right before its own `plan_run` INSERT -- see order_recon_job.py's own
    comment) -- so `recon.run` as a plan's ONLY/first step is exactly the
    "callee does not self-manage tenant context" case the fix must cover."""
    tenant = await create_test_tenant(db, name="Recon First Step Co")
    events = _spy_events(monkeypatch, db)

    real_spec = STEP_REGISTRY["recon.run"]

    async def recon_run_recording(ctx, params):
        events.append("exec:recon.run")
        return await real_spec.executor(ctx, params)

    monkeypatch.setitem(
        STEP_REGISTRY,
        "recon.run",
        StepSpec(
            type="recon.run",
            label=real_spec.label,
            kind="read",
            params_schema=real_spec.params_schema,
            executor=recon_run_recording,
        ),
    )

    now = datetime.now(timezone.utc)
    await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "recon.run", "params": {"window_days": 1}}]},
        next_run_at=now - timedelta(minutes=1),
        cron_expression="0 6 * * 1",
    )

    stats = await run_due_jobs(db, tenant.id, now=now)
    assert stats["ran"] == 1
    assert stats["failed"] == 0

    exec_idx = events.index("exec:recon.run")
    assert events[exec_idx - 1] == "ctx", (
        f"recon.run must run with tenant context freshly set immediately before it; "
        f"events around it: {events[max(0, exec_idx - 3) : exec_idx + 1]}"
    )


# ---------------------------------------------------------------------------
# Item 1 (delta gate fix #2): the compose step's own Drive identity stamp
# must survive a LATER step's failure -- `_run_steps` commits right before
# each WRITE step's executor runs, so anything the compose step (a read
# step) flushed is durable BEFORE drive.upload's own executor ever gets a
# chance to fail.
# ---------------------------------------------------------------------------


async def test_compose_stamped_delivery_identity_survives_a_later_drive_upload_failure(db: AsyncSession, monkeypatch):
    """A real `report.compose -> report.render_pdf -> report.build_xlsx ->
    drive.upload` plan where drive.upload raises (no `google_sheets`
    connector for the tenant -> `DeliveryUnavailable`, a REAL failure mode,
    not a fake one): after the run, the composed `Report` row's own
    `delivery_json["identity"]` must already be present -- stamped by
    `_report_compose_executor` right after compose returns and flushed
    there, made durable by drive.upload's own audit-before-call commit
    (the run loop's convention -- module docstring's "Idempotency +
    audit-before-call") before its executor ever runs (and fails).
    `report.render_pdf` is faked here (WeasyPrint's native libs are not
    guaranteed on this machine -- see report_pdf's own skip-probe);
    `report.compose` and `report.build_xlsx` are the REAL registry
    executors, composing a real inventory_aging report the same way
    `tests/test_report_playbooks.py`'s own `_patch_bigquery_executor` does."""
    from app.models.report import Report
    from tests.test_report_playbooks import _patch_bigquery_executor

    tenant = await create_test_tenant(db, name="ComposeIdentitySurvivesCo")
    # Captured now, not read off `tenant.id` after the run: a rollback inside
    # `_run_steps` (DeliveryUnavailable, here) expires every attribute on
    # every object in the session INCLUDING `tenant` -- nothing re-selects
    # Tenant afterward (unlike Schedule/Job, which `_finalize_run` always
    # re-fetches), so re-reading `tenant.id` post-run outside an awaited call
    # raises MissingGreenlet (the same failure mode this file's own
    # `test_step_error_retries_once_then_pauses` avoids the same way).
    tenant_id = tenant.id
    await set_tenant_context(db, str(tenant_id))
    _, _, params = _patch_bigquery_executor(monkeypatch)

    async def fake_render_pdf(ctx, step_params):
        artifact = ctx.artifacts[step_params["report_step"]]
        return {"pdf_bytes": b"%PDF-FAKE", "report_id": artifact["report_id"]}

    monkeypatch.setitem(
        STEP_REGISTRY, "report.render_pdf", _fake_spec("read", fake_render_pdf, step_type="report.render_pdf")
    )

    now = datetime.now(timezone.utc)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={
            "steps": [
                {
                    "id": "compose",
                    "type": "report.compose",
                    "params": {"playbook_key": "inventory_aging", "params": params},
                },
                {"id": "render_pdf", "type": "report.render_pdf", "params": {"report_step": "compose"}},
                {"id": "build_xlsx", "type": "report.build_xlsx", "params": {"report_step": "compose"}},
                {"id": "upload", "type": "drive.upload", "params": {"report_step": "compose"}},
            ]
        },
        next_run_at=now - timedelta(minutes=1),
        cron_expression="0 6 * * 1",
    )

    stats = await run_due_jobs(db, tenant_id, now=now)
    assert stats["ran"] == 1

    jobs = (await db.execute(select(Job).where(Job.tenant_id == tenant_id))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].result_summary["reason"] == REASON_BLOCKED  # no connector -> DeliveryUnavailable
    assert schedule.last_run_status == REASON_BLOCKED

    report_row = (
        await db.execute(select(Report).where(Report.tenant_id == tenant_id, Report.title == "Inventory Aging Weekly"))
    ).scalar_one()
    assert report_row.delivery_json is not None
    identity = report_row.delivery_json["identity"]
    assert identity["folder_props"] == {"schedule_id": str(schedule.id)}
    assert identity["file_props"] == {"schedule_id": str(schedule.id), "report_step": "compose"}
    assert identity["lock_key"] == f"schedule:{schedule.id}:compose"
    assert identity["idempotency_prefix"] == f"job-delivery:{schedule.id}:compose"


async def test_a_successful_steps_writes_survive_a_later_read_steps_failure(db: AsyncSession, monkeypatch):
    """Item 2 (delta gate fix E): a successful step's writes were only
    FLUSHED, not committed -- a plan whose steps after `report.compose` are
    all READS (no `drive.upload`/other write step to cover it with its own
    audit-before-call commit) had NOTHING durable at all. A later read step
    (`report.build_xlsx`, here faked to raise) rolling the transaction back
    therefore undid `report.compose`'s own identity stamp too, even though
    compose itself had already succeeded. Fixed in `_run_steps`: commit
    after EVERY successful step (not just before a write step's call)."""
    from app.models.report import Report
    from tests.test_report_playbooks import _patch_bigquery_executor

    tenant = await create_test_tenant(db, name="StepCommitSurvivesCo")
    tenant_id = tenant.id
    await set_tenant_context(db, str(tenant_id))
    _, _, params = _patch_bigquery_executor(monkeypatch)

    async def raising_build_xlsx(ctx, step_params):
        raise StepExecutionError("simulated build_xlsx failure -- a pure read step, no write step involved")

    monkeypatch.setitem(
        STEP_REGISTRY, "report.build_xlsx", _fake_spec("read", raising_build_xlsx, step_type="report.build_xlsx")
    )

    now = datetime.now(timezone.utc)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={
            "steps": [
                {
                    "id": "compose",
                    "type": "report.compose",
                    "params": {"playbook_key": "inventory_aging", "params": params},
                },
                {"id": "build_xlsx", "type": "report.build_xlsx", "params": {"report_step": "compose"}},
            ]
        },
        next_run_at=now - timedelta(minutes=1),
        cron_expression="0 6 * * 1",
    )

    stats = await run_due_jobs(db, tenant_id, now=now)
    assert stats["failed"] == 1

    jobs = (await db.execute(select(Job).where(Job.tenant_id == tenant_id))).scalars().all()
    attempt1_job = next(j for j in jobs if j.parameters["attempt"] == 1)
    assert attempt1_job.result_summary["reason"] == REASON_ERROR

    # Re-read the Report row FRESH -- `report.compose`'s own identity stamp
    # must be durable even though the only step after it (a pure read, no
    # write step ever ran) raised and rolled its own attempt back.
    report_row = (
        await db.execute(select(Report).where(Report.tenant_id == tenant_id, Report.title == "Inventory Aging Weekly"))
    ).scalar_one()
    assert report_row.delivery_json is not None
    assert "identity" in report_row.delivery_json


# ---------------------------------------------------------------------------
# `_finalize_run`'s OWN retry (the one after a first finalize crash) must
# itself be guarded (review finding, MINOR): if it ALSO fails, the exception
# was propagating straight out of `run_schedule_now`, leaving the `jobs` row
# stuck at status="running" forever with `next_run_at` already advanced and
# no record of what happened.
# ---------------------------------------------------------------------------


async def test_finalize_double_failure_does_not_leave_the_job_row_running(db: AsyncSession, monkeypatch):
    tenant = await create_test_tenant(db, name="Finalize Double Crash Co")
    tenant_id = tenant.id
    await set_tenant_context(db, str(tenant_id))

    async def ok_step(ctx, params):
        return {"ok": True}

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", ok_step))

    now = datetime.now(timezone.utc)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=now - timedelta(minutes=1),
        cron_expression="0 6 * * 1",
    )
    schedule_id = schedule.id

    async def always_flaky_finalize(*args, **kwargs):
        # BOTH calls fail -- the first attempt AND its retry -- simulating
        # whatever broke the first time (a stale row, a connectivity blip)
        # still being broken on the retry. Mirrors the REAL `_finalize_run`'s
        # own first action (re-establish tenant context) before failing
        # deeper in -- item 2 (delta gate fix E) now commits after
        # `_run_steps`'s lone read step succeeds, clearing `SET LOCAL
        # app.current_tenant_id`; a fake that skipped this (unlike the real
        # function) would leave the eventual raw-SQL fallback below running
        # with no tenant context at all, and its RLS-scoped UPDATE would
        # silently affect zero rows.
        await set_tenant_context(db, str(tenant_id))
        raise RuntimeError("simulated crash re-fetching Schedule/Job (never recovers)")

    monkeypatch.setattr(scheduled_jobs, "_finalize_run", always_flaky_finalize)

    stats = await run_due_jobs(db, tenant_id, now=now)

    assert stats["ran"] == 1
    assert stats["failed"] == 1

    # The jobs row must never be left stuck at status="running" -- even
    # when BOTH the finalize call and its one retry crash.
    job = (await db.execute(select(Job).where(Job.tenant_id == tenant_id))).scalar_one()
    # The raw-SQL fallback must land on the same vocabulary every other writer
    # uses (pending/running/completed/failed) so a doubly-failed run still shows
    # up in ?status=failed filters and failed-job counts.
    assert job.status == "failed"
    assert job.result_summary is not None
    assert job.result_summary["reason"] == REASON_ERROR

    refreshed_schedule = (await db.execute(select(Schedule).where(Schedule.id == schedule_id))).scalar_one()
    assert refreshed_schedule.last_run_status == REASON_ERROR


# ---------------------------------------------------------------------------
# Retry-then-pause applies to SWEEP-started occurrences only (review finding,
# MAJOR): `run_schedule_now`'s `reason == REASON_ERROR` branch used to fire
# for EVERY caller -- an operator's "Run now" (Celery path, actor_type=
# "user") or the MCP tool failing overwrote `next_run_at` with `now + 15 min`
# (clobbering the real next cron occurrence) and marked `retry_pending`, so
# the sweep later replayed the APPROVED plan even when the failed run was a
# `use_pending=True` preview. `retry_on_error` (default False) gates this;
# only `run_due_jobs` (the sweep) passes `True`.
# ---------------------------------------------------------------------------


async def test_run_schedule_now_default_does_not_retry_or_touch_next_run_at_on_error(db: AsyncSession, monkeypatch):
    """Mirrors the Celery "Run now" path (`run_schedule_now_task`) and the
    MCP `schedule.run` tool -- neither passes `retry_on_error`, so both get
    the default `False`. A failing run must stamp the jobs row + schedule's
    `last_run_status="error"` (via `_finalize_run`, as always) WITHOUT
    scheduling the 15-minutes-later retry or touching `next_run_at`/
    `paused_at`/`pause_reason` at all."""
    tenant = await create_test_tenant(db, name="Manual Run No Retry Co")
    tenant_id = tenant.id  # captured now -- a step's StepExecutionError triggers a
    # real rollback (_run_steps), which expires every object in the session's
    # identity map, `tenant` included; re-reading an expired attribute outside
    # an awaited DB call raises MissingGreenlet (same class of bug documented
    # on test_step_error_retries_once_then_pauses above).
    await set_tenant_context(db, str(tenant_id))

    async def always_fails(ctx, params):
        raise StepExecutionError("boom: manual run failed")

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", always_fails))

    original_next_run_at = datetime(2026, 12, 25, 6, 0, tzinfo=timezone.utc)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=original_next_run_at,
        cron_expression="0 6 * * 1",
    )
    schedule_id = schedule.id

    outcome = await run_schedule_now(
        db, schedule_id, tenant_id=tenant_id, actor_id=None, actor_type="user", use_pending=False
    )

    assert outcome.reason == REASON_ERROR

    refreshed = (await db.execute(select(Schedule).where(Schedule.id == schedule_id))).scalar_one()
    assert refreshed.last_run_status == "error"  # not "retry_pending", not "paused"
    assert refreshed.next_run_at == original_next_run_at  # untouched
    assert refreshed.paused_at is None
    assert refreshed.pause_reason is None

    job = (await db.execute(select(Job).where(Job.tenant_id == tenant_id))).scalar_one()
    assert job.status == "failed"
    assert job.result_summary["reason"] == REASON_ERROR


async def test_run_due_jobs_still_schedules_the_retry_on_error(db: AsyncSession, monkeypatch):
    """The sweep path (`run_due_jobs` -> `retry_on_error=True`) must keep the
    existing retry-then-pause behaviour exactly as before this fix."""
    tenant = await create_test_tenant(db, name="Sweep Still Retries Co")
    await set_tenant_context(db, str(tenant.id))

    async def always_fails(ctx, params):
        raise StepExecutionError("boom: sweep run failed")

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", always_fails))

    now = datetime.now(timezone.utc)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=now - timedelta(minutes=1),
        cron_expression="0 6 * * 1",
    )

    stats = await run_due_jobs(db, tenant.id, now=now)
    assert stats["failed"] == 1
    assert schedule.last_run_status == "retry_pending"
    assert schedule.next_run_at is not None
    assert schedule.next_run_at - now >= timedelta(minutes=14)
    assert schedule.next_run_at - now <= timedelta(minutes=16)


# ---------------------------------------------------------------------------
# Item 1 (delta gate fix): the retry is an explicit `schedules.retry_job_id`
# column now, never a JSON query on `jobs` ordered by `started_at`. That old
# query broke in three ways at once: a BLOCKED attempt-1 row with
# `started_at IS NULL` sorts FIRST under `DESC`, a manual "Run now" also
# creates an `attempt=1` row it couldn't tell apart from the sweep's own, and
# a manual failure overwrites `last_run_status` (which the claim ALSO read to
# decide "is this the retry?"). The retry's `jobs` row is now created EAGERLY
# at SCHEDULING time (`run_schedule_now`'s retry-then-pause branch), and
# `_claim_due_schedules` reuses it via `retry_job_id`, clearing the column in
# the same claim transaction -- see the four tests below.
# ---------------------------------------------------------------------------


async def test_retry_reuses_attempt_ones_period_key_across_local_midnight(db: AsyncSession, monkeypatch):
    tenant = await create_test_tenant(db, name="Retry Period Key Co")
    tenant_id = tenant.id
    await set_tenant_context(db, str(tenant_id))

    async def always_fails(ctx, params):
        raise StepExecutionError("boom: always fails")

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", always_fails))

    # Sunday 2026-09-06 23:50 America/Los_Angeles (PDT, UTC-7) == Monday
    # 2026-09-07 06:50 UTC. Attempt 1 fails here; the 15-minutes-later retry
    # (now + RETRY_DELAY_MINUTES == exactly 07:05 UTC) is Monday 00:05
    # local -- crossing local midnight.
    due_at1 = datetime(2026, 9, 7, 6, 50, tzinfo=timezone.utc)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=due_at1,
        cron_expression="0 6 * * 1",
        tz="America/Los_Angeles",
    )
    schedule_id = schedule.id

    stats1 = await run_due_jobs(db, tenant_id, now=due_at1)
    assert stats1["failed"] == 1

    refreshed = (await db.execute(select(Schedule).where(Schedule.id == schedule_id))).scalar_one()
    assert refreshed.last_run_status == "retry_pending"
    assert refreshed.retry_job_id is not None
    due_at2 = refreshed.next_run_at
    assert due_at2 == datetime(2026, 9, 7, 7, 5, tzinfo=timezone.utc)  # Monday 00:05 PDT

    # The retry's jobs row already exists -- created eagerly at scheduling
    # time -- with the CORRECT period_key/retry_of_job_id already on it,
    # never derived from a query at claim/run time.
    jobs_after_1 = (await db.execute(select(Job).where(Job.tenant_id == tenant_id))).scalars().all()
    assert len(jobs_after_1) == 2
    attempt1_job = next(j for j in jobs_after_1 if j.parameters["attempt"] == 1)
    retry_job = next(j for j in jobs_after_1 if j.parameters["attempt"] == 2)
    assert retry_job.id == refreshed.retry_job_id
    assert retry_job.status == "pending"  # not run yet
    attempt1_period_key = attempt1_job.parameters["period_key"]
    assert attempt1_period_key == "2026-09-06"  # Sunday, local date
    attempt1_job_id = attempt1_job.id  # captured now -- the next run_due_jobs commits, expiring this object
    retry_job_id = retry_job.id
    assert retry_job.parameters["period_key"] == attempt1_period_key == "2026-09-06"
    assert retry_job.parameters["retry_of_job_id"] == str(attempt1_job_id)

    now2 = due_at2 + timedelta(seconds=1)
    stats2 = await run_due_jobs(db, tenant_id, now=now2)
    assert stats2["failed"] == 1

    # The naive computation (due_at2's own local date) would be Monday
    # 2026-09-07 -- a DIFFERENT day than attempt 1's. period_key/retry_of_job_id
    # survive on the completed retry row exactly as pre-created.
    completed_retry = (await db.execute(select(Job).where(Job.id == retry_job_id))).scalar_one()
    assert completed_retry.status == "failed"
    assert completed_retry.parameters["period_key"] == attempt1_period_key == "2026-09-06"
    assert completed_retry.parameters["retry_of_job_id"] == str(attempt1_job_id)


async def test_retry_attribution_survives_a_stale_null_started_at_row_and_a_manual_run(db: AsyncSession, monkeypatch):
    """review finding, MAJOR: the OLD JSON-query mechanism attributed the
    retry via `Job.parameters["attempt"].astext == "1"` ORDER BY `started_at`
    DESC — a stale BLOCKED attempt-1 row with `started_at IS NULL` sorts
    FIRST under `DESC` (Postgres: NULLS FIRST for DESC), and a manual "Run
    now" landing in between ALSO creates an `attempt=1` row and flips
    `last_run_status` away from `retry_pending` — either one could make the
    retry pick up the WRONG occurrence's `period_key`/`retry_of_job_id`. The
    new `retry_job_id` column is structurally immune: neither noise row is
    ever consulted."""
    tenant = await create_test_tenant(db, name="Retry Attribution Co")
    tenant_id = tenant.id
    await set_tenant_context(db, str(tenant_id))

    async def always_fails(ctx, params):
        raise StepExecutionError("boom: sweep run fails")

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", always_fails))

    now = datetime.now(timezone.utc)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=now - timedelta(minutes=1),
        cron_expression="0 6 * * 1",
    )
    schedule_id = schedule.id

    # A stale BLOCKED attempt-1 row with started_at IS NULL -- exactly what
    # the old bug's ORDER BY started_at DESC would have surfaced FIRST.
    stale_blocked = Job(
        tenant_id=tenant_id,
        job_type="scheduled_job",
        status="completed",
        started_at=None,
        parameters={"schedule_id": str(schedule_id), "attempt": 1, "period_key": "1999-01-01"},
        result_summary={"reason": REASON_BLOCKED, "outputs": {}, "detail": "stale"},
    )
    db.add(stale_blocked)
    await db.commit()

    # The real sweep-triggered attempt 1: fails, schedules the retry.
    stats1 = await run_due_jobs(db, tenant_id, now=now)
    assert stats1["failed"] == 1

    refreshed = (await db.execute(select(Schedule).where(Schedule.id == schedule_id))).scalar_one()
    retry_at = refreshed.next_run_at
    jobs_after_1 = (await db.execute(select(Job).where(Job.tenant_id == tenant_id))).scalars().all()
    real_attempt1 = next(j for j in jobs_after_1 if j.id != stale_blocked.id and j.parameters.get("attempt") == 1)
    retry_job = next(j for j in jobs_after_1 if j.parameters.get("attempt") == 2)
    real_attempt1_id = real_attempt1.id  # captured now -- the next commit expires these objects
    retry_job_id = retry_job.id
    assert retry_job.parameters["retry_of_job_id"] == str(real_attempt1_id)
    assert retry_job.parameters["period_key"] == real_attempt1.parameters["period_key"]

    # A manual "Run now" lands BEFORE the retry fires -- also attempt=1
    # (its own fresh row, no existing_job_id), also fails, and per item 1's
    # own rule flips last_run_status to "error" WITHOUT touching
    # retry_job_id (manual runs never read/write it).
    await run_schedule_now(db, schedule_id, tenant_id=tenant_id, actor_id=None, actor_type="user", use_pending=False)

    refreshed_after_manual = (await db.execute(select(Schedule).where(Schedule.id == schedule_id))).scalar_one()
    assert refreshed_after_manual.last_run_status == "error"
    assert refreshed_after_manual.retry_job_id == retry_job_id  # untouched by the manual run
    assert refreshed_after_manual.next_run_at == retry_at  # untouched by the manual run

    # The scheduled retry, when it fires, must still resolve to the REAL
    # attempt 1's occurrence -- never the stale row, never the manual run.
    now2 = retry_at + timedelta(seconds=1)
    stats2 = await run_due_jobs(db, tenant_id, now=now2)
    assert stats2["ran"] == 1
    assert stats2["failed"] == 1

    final_jobs = (await db.execute(select(Job).where(Job.tenant_id == tenant_id))).scalars().all()
    assert len(final_jobs) == 4  # stale + real attempt1 + retry + the manual run's own row -- no extra
    completed_retry = next(j for j in final_jobs if j.id == retry_job_id)
    real_attempt1_refreshed = next(j for j in final_jobs if j.id == real_attempt1_id)
    assert completed_retry.status == "failed"
    assert completed_retry.parameters["retry_of_job_id"] == str(real_attempt1_id)
    assert completed_retry.parameters["period_key"] == real_attempt1_refreshed.parameters["period_key"]


async def test_sweep_claim_clears_retry_job_id_and_reuses_the_row_no_third_row(db: AsyncSession, monkeypatch):
    """After the sweep claims the pending retry, `retry_job_id` must be NULL
    (cleared in the same claim transaction) and the reused row -- never a
    third one -- is the one that completes."""
    tenant = await create_test_tenant(db, name="Retry Claim Clears Co")
    tenant_id = tenant.id
    await set_tenant_context(db, str(tenant_id))

    async def always_fails(ctx, params):
        raise StepExecutionError("boom: always fails")

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", always_fails))

    now = datetime.now(timezone.utc)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=now - timedelta(minutes=1),
        cron_expression="0 6 * * 1",
    )
    schedule_id = schedule.id

    stats1 = await run_due_jobs(db, tenant_id, now=now)
    assert stats1["failed"] == 1

    refreshed = (await db.execute(select(Schedule).where(Schedule.id == schedule_id))).scalar_one()
    assert refreshed.retry_job_id is not None
    retry_job_id = refreshed.retry_job_id
    retry_at = refreshed.next_run_at

    now2 = retry_at + timedelta(seconds=1)
    stats2 = await run_due_jobs(db, tenant_id, now=now2)
    assert stats2["ran"] == 1

    refreshed2 = (await db.execute(select(Schedule).where(Schedule.id == schedule_id))).scalar_one()
    assert refreshed2.retry_job_id is None  # cleared at claim time

    jobs = (await db.execute(select(Job).where(Job.tenant_id == tenant_id))).scalars().all()
    assert len(jobs) == 2  # attempt 1 + the retry -- NO third row
    completed_retry = next(j for j in jobs if j.id == retry_job_id)
    assert completed_retry.status == "failed"


async def test_finalize_run_schedule_select_carries_for_update_lock(db: AsyncSession, monkeypatch):
    """Item 3 (delta gate fix E): the one-pending-retry guard
    (`if row.retry_job_id is not None: skip`) is check-then-act on a
    Schedule row `_finalize_run` selects -- without a row lock, two
    overlapping failing occurrences can both read NULL and both create a
    retry. The SELECT must carry a PLAIN `FOR UPDATE` (never SKIP LOCKED --
    the second occurrence must WAIT, then see the first's `retry_job_id`).
    Intercepts `db.execute` to capture the statement `_finalize_run` issues,
    compiles it against the PostgreSQL dialect, and asserts `FOR UPDATE`
    appears -- the same technique test_metric_authoring_db.py's own
    test_update_metric_select_carries_for_update_lock uses for the
    identical class of lost-update race."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.sql import Select

    tenant = await create_test_tenant(db, name="FinalizeLockCheckCo")
    tenant_id = tenant.id
    await set_tenant_context(db, str(tenant_id))
    now = datetime.now(timezone.utc)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=now,
        cron_expression="0 6 * * 1",
    )
    job = Job(
        tenant_id=tenant_id,
        job_type="scheduled_job",
        status="running",
        parameters={"schedule_id": str(schedule.id), "attempt": 1},
    )
    db.add(job)
    await db.flush()
    await db.commit()
    await set_tenant_context(db, str(tenant_id))

    captured_stmts: list = []
    real_execute = db.execute

    async def intercepting_execute(stmt, *args, **kwargs):
        captured_stmts.append(stmt)
        return await real_execute(stmt, *args, **kwargs)

    monkeypatch.setattr(db, "execute", intercepting_execute)

    await scheduled_jobs._finalize_run(
        db,
        schedule_id=schedule.id,
        tenant_id=tenant_id,
        job_id_value=job.id,
        reason=REASON_DONE,
        outputs={},
        detail=None,
        now=now,
    )

    # `_finalize_run`'s FIRST `db.execute` call is its own `set_tenant_context`
    # (a raw `text(...)` SET LOCAL) -- filter to the actual Schedule SELECT
    # rather than relying on call position.
    select_stmts = [s for s in captured_stmts if isinstance(s, Select)]
    assert select_stmts, f"_finalize_run did not issue any SELECT; captured: {captured_stmts}"
    first_stmt = select_stmts[0]
    compiled = first_stmt.compile(dialect=postgresql.dialect())
    sql_text = str(compiled)
    assert "FOR UPDATE" in sql_text.upper(), (
        f"Expected the Schedule SELECT inside _finalize_run to carry FOR UPDATE, got:\n{sql_text}"
    )


# ---------------------------------------------------------------------------
# Item 3 (delta gate fix): one pending retry per schedule, never an orphan.
# Two overlapping occurrences of the SAME schedule (a run longer than its
# cron interval) that both fail race to set `retry_job_id` -- the LATER
# assignment used to orphan the EARLIER occurrence's pending retry row.
# ---------------------------------------------------------------------------


async def test_two_overlapping_failures_never_orphan_the_pending_retry(db: AsyncSession, monkeypatch):
    """Simulates the race directly: two DIFFERENT attempt=1 occurrences of
    the same schedule (e.g. a run longer than its cron interval, so the
    sweep claims a SECOND occurrence before the first one's failure has
    assigned `retry_job_id`) both fail. The SECOND to finalize must not
    create a second retry row or touch `next_run_at`/`retry_job_id` -- it
    writes a `jobs.retry.skipped` audit and leaves `last_run_status="error"`."""
    tenant = await create_test_tenant(db, name="Overlapping Retry Race Co")
    tenant_id = tenant.id
    await set_tenant_context(db, str(tenant_id))

    async def always_fails(ctx, params):
        raise StepExecutionError("boom: overlapping occurrence fails")

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", always_fails))

    now = datetime.now(timezone.utc)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=now - timedelta(minutes=1),
        cron_expression="0 6 * * 1",
    )
    schedule_id = schedule.id

    # First occurrence -- attempt=1, retry_on_error=True. Both occurrences
    # here are simulated as direct run_schedule_now calls (not via
    # run_due_jobs's own claim, which would not naturally produce two
    # concurrent attempt=1 claims in this single-threaded test) -- exactly
    # what the sweep itself passes for every occurrence it runs.
    outcome1 = await run_schedule_now(
        db,
        schedule_id,
        tenant_id=tenant_id,
        actor_id=None,
        actor_type="system",
        due_at=now,
        now=now,
        attempt=1,
        retry_on_error=True,
    )
    assert outcome1.reason == REASON_ERROR

    refreshed1 = (await db.execute(select(Schedule).where(Schedule.id == schedule_id))).scalar_one()
    assert refreshed1.retry_job_id is not None
    first_retry_job_id = refreshed1.retry_job_id
    first_retry_at = refreshed1.next_run_at

    # Second, OVERLAPPING occurrence -- also attempt=1 (claimed before the
    # first occurrence's failure assigned retry_job_id), also fails.
    now2 = now + timedelta(seconds=5)
    outcome2 = await run_schedule_now(
        db,
        schedule_id,
        tenant_id=tenant_id,
        actor_id=None,
        actor_type="system",
        due_at=now2,
        now=now2,
        attempt=1,
        retry_on_error=True,
    )
    assert outcome2.reason == REASON_ERROR

    refreshed2 = (await db.execute(select(Schedule).where(Schedule.id == schedule_id))).scalar_one()
    assert refreshed2.retry_job_id == first_retry_job_id  # unchanged -- no second retry row assigned
    assert refreshed2.next_run_at == first_retry_at  # unchanged
    assert refreshed2.last_run_status == "error"  # not overwritten back to retry_pending

    jobs = (await db.execute(select(Job).where(Job.tenant_id == tenant_id))).scalars().all()
    pending_retry_jobs = [j for j in jobs if j.parameters.get("attempt") == 2]
    assert len(pending_retry_jobs) == 1  # exactly one pending attempt-2 row, never a second
    assert pending_retry_jobs[0].id == first_retry_job_id

    skipped_events = (
        (
            await db.execute(
                select(AuditEvent).where(AuditEvent.tenant_id == tenant_id, AuditEvent.action == "jobs.retry.skipped")
            )
        )
        .scalars()
        .all()
    )
    assert len(skipped_events) == 1
    assert skipped_events[0].payload["pending_retry_job_id"] == str(first_retry_job_id)
    assert skipped_events[0].payload["job_id"] == str(outcome2.jobs_row_id)
    assert skipped_events[0].status == "error"


async def test_attempt_exhaustion_pauses_even_when_another_occurrence_has_a_pending_retry(
    db: AsyncSession, monkeypatch
):
    """Item 4 (delta gate fix E): the pending-retry guard used to sit AHEAD
    of the `attempt >= RETRY_MAX_ATTEMPTS` check, so an occurrence that has
    ITSELF exhausted its retry silently skipped the pause + `jobs.paused`
    audit whenever some OTHER occurrence's `retry_job_id` happened to be
    set. Simulated directly: seed a schedule whose `retry_job_id` already
    points at some OTHER occurrence's pending retry job, then run THIS
    occurrence at attempt=2 (its own final retry) and let it fail too -- it
    must still pause, never silently emit `jobs.retry.skipped` instead."""
    tenant = await create_test_tenant(db, name="ExhaustionPausesAnywayCo")
    tenant_id = tenant.id
    await set_tenant_context(db, str(tenant_id))

    async def always_fails(ctx, params):
        raise StepExecutionError("boom: attempt 2 fails too")

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", always_fails))

    now = datetime.now(timezone.utc)
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=now - timedelta(minutes=1),
        cron_expression="0 6 * * 1",
    )
    schedule_id = schedule.id

    # Simulate: some OTHER occurrence's retry is already pending -- a real
    # pending Job row, with `retry_job_id` pointing at it directly (never
    # created via THIS occurrence's own run).
    other_pending = Job(
        tenant_id=tenant_id,
        job_type="scheduled_job",
        status="pending",
        parameters={"schedule_id": str(schedule_id), "attempt": 2, "period_key": "2026-09-01"},
    )
    db.add(other_pending)
    await db.flush()
    schedule.retry_job_id = other_pending.id
    await db.commit()
    await set_tenant_context(db, str(tenant_id))

    # THIS occurrence is itself attempt=2 -- its OWN final retry, exhausted
    # on failure -- and must pause regardless of `retry_job_id` pointing at
    # the (unrelated) other_pending row.
    outcome = await run_schedule_now(
        db,
        schedule_id,
        tenant_id=tenant_id,
        actor_id=None,
        actor_type="system",
        due_at=now,
        now=now,
        attempt=2,
        retry_on_error=True,
    )
    assert outcome.reason == REASON_ERROR

    refreshed = (await db.execute(select(Schedule).where(Schedule.id == schedule_id))).scalar_one()
    assert refreshed.paused_at is not None
    assert refreshed.last_run_status == "paused"

    pause_events = (
        (await db.execute(select(AuditEvent).where(AuditEvent.tenant_id == tenant_id, AuditEvent.action == "jobs.paused")))
        .scalars()
        .all()
    )
    assert len(pause_events) == 1

    skipped_events = (
        (
            await db.execute(
                select(AuditEvent).where(AuditEvent.tenant_id == tenant_id, AuditEvent.action == "jobs.retry.skipped")
            )
        )
        .scalars()
        .all()
    )
    assert skipped_events == []  # must NOT silently skip -- this occurrence's own attempts are exhausted


async def test_period_key_falls_back_to_computed_value_when_not_supplied(db: AsyncSession, monkeypatch):
    """Defensive only -- a caller invoking `run_schedule_now` directly with
    `attempt=2` but no `period_key` (e.g. the MCP tool) must not crash; it
    falls back to the same due_at-derived computation attempt 1 uses."""
    tenant = await create_test_tenant(db, name="Retry No PeriodKey Co")
    await set_tenant_context(db, str(tenant.id))

    async def fake_exec(ctx, params):
        return {"ok": True}

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", fake_exec))

    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=None,
        cron_expression="0 6 * * 1",
        tz="UTC",
    )

    due_at = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    outcome = await run_schedule_now(db, schedule.id, tenant_id=tenant.id, actor_id=None, due_at=due_at, attempt=2)

    assert outcome.reason == REASON_DONE
    job = (await db.execute(select(Job).where(Job.tenant_id == tenant.id))).scalar_one()
    assert job.parameters["period_key"] == "2026-09-07"  # computed from due_at, as before
    assert job.parameters.get("retry_of_job_id") is None


# ---------------------------------------------------------------------------
# recon.run's window follows the SCHEDULE's own timezone (review finding):
# `_recon_run_executor` (registry.py) used a naive `date.today()` -- the
# server/UTC wall clock -- instead of the run's own `period_key` (spec §B4),
# which `run_schedule_now` already computes in the schedule's timezone. Near
# midnight UTC a non-UTC schedule's "today" and the UTC server's "today"
# disagree by a day.
# ---------------------------------------------------------------------------


async def test_recon_run_uses_the_schedule_timezone_local_date_not_utc_date(db: AsyncSession, monkeypatch):
    tenant = await create_test_tenant(db, name="Recon TZ Co")
    await set_tenant_context(db, str(tenant.id))

    captured: list[dict] = []

    async def fake_execute(db_, **kwargs):
        captured.append(kwargs)
        return {"ok": True}

    import app.workers.tasks.reconciliation_run as recon_run_module

    monkeypatch.setattr(recon_run_module, "_execute", fake_execute)

    # 2026-09-09 05:00 UTC == 2026-09-08 22:00 America/Los_Angeles (PDT,
    # UTC-7) -- still "yesterday" in the schedule's own timezone, so a naive
    # date.today() (server/UTC wall clock) disagrees with the schedule's own
    # local date at the moment this test runs.
    due_at = datetime(2026, 9, 9, 5, 0, tzinfo=timezone.utc)
    await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "recon.run", "params": {"window_days": 7}}]},
        next_run_at=due_at,
        cron_expression="0 6 * * *",
        tz="America/Los_Angeles",
    )

    stats = await run_due_jobs(db, tenant.id, now=due_at)
    assert stats["ran"] == 1
    assert stats["failed"] == 0

    assert len(captured) == 1
    assert captured[0]["date_to"] == "2026-09-08"
    assert captured[0]["date_from"] == "2026-09-01"


# ---------------------------------------------------------------------------
# run_schedule_now(existing_job_id=...) reuses that jobs row instead of
# inserting a second one (Task 5 residual: the request-scoped "Run now"
# endpoint now enqueues via Celery instead of executing inline; it creates
# the jobs row itself, up front, so its 202 response can carry a real id
# immediately, then dispatches a task that calls run_schedule_now with that
# row's id).
# ---------------------------------------------------------------------------


async def test_run_schedule_now_reuses_an_existing_jobs_row_when_given_one(db: AsyncSession, monkeypatch):
    tenant = await create_test_tenant(db, name="Existing Job Co")
    await set_tenant_context(db, str(tenant.id))

    async def fake_exec(ctx, params):
        return {"ok": True}

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", fake_exec))

    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=None,
        cron_expression="0 6 * * 1",
    )

    # Mirrors what the API endpoint does before dispatching the Celery task:
    # create a placeholder jobs row up front.
    pre_created = Job(
        tenant_id=tenant.id,
        job_type="scheduled_job",
        status="pending",
        parameters={"schedule_id": str(schedule.id), "use_pending": False},
    )
    db.add(pre_created)
    await db.flush()
    await db.commit()
    pre_created_id = pre_created.id

    outcome = await run_schedule_now(
        db,
        schedule.id,
        tenant_id=tenant.id,
        actor_id=None,
        use_pending=False,
        existing_job_id=pre_created_id,
    )

    assert outcome.reason == REASON_DONE
    assert outcome.jobs_row_id == pre_created_id

    jobs = (await db.execute(select(Job).where(Job.tenant_id == tenant.id))).scalars().all()
    assert len(jobs) == 1, f"expected the SAME jobs row to be reused, got {len(jobs)}"
    assert jobs[0].id == pre_created_id
    assert jobs[0].status == "completed"
    assert jobs[0].result_summary["reason"] == REASON_DONE
    assert jobs[0].parameters["schedule_id"] == str(schedule.id)


async def test_run_now_uses_the_plan_snapshotted_on_the_jobs_row_not_a_later_edit(db: AsyncSession, monkeypatch):
    """review finding, MAJOR: `run_schedule_now` used to read
    `row.pending_plan_json`/`row.plan_json` LIVE when the Celery task
    executed -- an instruction edit or discard landing between enqueue
    (`POST /schedules/{id}/run`) and execution silently changed what ran.
    The endpoint now snapshots the validated plan onto the pre-created jobs
    row's `parameters["plan"]` (see the API test); `run_schedule_now`, given
    `existing_job_id`, must replay THAT snapshot instead of the schedule's
    current live plan."""
    tenant = await create_test_tenant(db, name="Plan Snapshot Co")
    await set_tenant_context(db, str(tenant.id))

    async def fake_exec(ctx, params):
        return {"label": params.get("label")}

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", fake_exec))

    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1_original", "type": "fake.step", "params": {"label": "original"}}]},
        next_run_at=None,
        cron_expression="0 6 * * 1",
    )

    snapshot_plan = {"steps": [{"id": "s1_snapshot", "type": "fake.step", "params": {"label": "snapshot"}}]}
    pre_created = Job(
        tenant_id=tenant.id,
        job_type="scheduled_job",
        status="pending",
        parameters={"schedule_id": str(schedule.id), "use_pending": False, "plan": snapshot_plan},
    )
    db.add(pre_created)
    await db.flush()
    await db.commit()
    pre_created_id = pre_created.id

    # An instruction edit lands between enqueue and execution -- the live
    # schedule row now has a COMPLETELY DIFFERENT plan than the snapshot.
    schedule.plan_json = {"steps": [{"id": "s1_edited", "type": "fake.step", "params": {"label": "edited"}}]}
    await db.commit()

    outcome = await run_schedule_now(
        db,
        schedule.id,
        tenant_id=tenant.id,
        actor_id=None,
        use_pending=False,
        existing_job_id=pre_created_id,
    )

    assert outcome.reason == REASON_DONE
    assert "s1_snapshot" in outcome.outputs
    assert "s1_edited" not in outcome.outputs

    job = (await db.execute(select(Job).where(Job.id == pre_created_id))).scalar_one()
    assert "s1_snapshot" in job.result_summary["outputs"]
    assert "s1_edited" not in job.result_summary["outputs"]


async def test_run_now_preserves_the_enqueue_time_plan_version_after_a_later_plan_version_bump(
    db: AsyncSession, monkeypatch
):
    """Item 4 (delta gate fix): `run_schedule_now` used to overwrite
    `job.parameters` WHOLESALE, replacing `plan_version` with the schedule's
    CURRENT value even when the pre-created row already snapshotted the
    plan_version the operator actually validated at enqueue time -- the
    completed job no longer said what version actually ran. The fix merges
    onto the existing row's own parameters instead: `plan`/`plan_version`
    survive when already present; only the runtime facts (period_key,
    attempt, use_pending, schedule_id) are always overwritten."""
    tenant = await create_test_tenant(db, name="Plan Version Snapshot Co")
    await set_tenant_context(db, str(tenant.id))

    async def fake_exec(ctx, params):
        return {"ok": True}

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", fake_exec))

    snapshot_plan = {"steps": [{"id": "s1", "type": "fake.step", "params": {}}]}
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json=snapshot_plan,
        next_run_at=None,
        cron_expression="0 6 * * 1",
        plan_version=1,
    )

    # Mirrors `schedule_service.enqueue_run`'s own pre-created row shape:
    # plan_version + plan snapshotted at ENQUEUE time.
    pre_created = Job(
        tenant_id=tenant.id,
        job_type="scheduled_job",
        status="pending",
        parameters={"schedule_id": str(schedule.id), "plan_version": 1, "use_pending": False, "plan": snapshot_plan},
    )
    db.add(pre_created)
    await db.flush()
    await db.commit()
    pre_created_id = pre_created.id

    # The schedule's plan_version is bumped AFTER enqueue (e.g. an instruction
    # edit was approved) -- before the Celery task actually executes.
    schedule.plan_version = 2
    await db.commit()

    outcome = await run_schedule_now(
        db, schedule.id, tenant_id=tenant.id, actor_id=None, use_pending=False, existing_job_id=pre_created_id
    )
    assert outcome.reason == REASON_DONE

    job = (await db.execute(select(Job).where(Job.id == pre_created_id))).scalar_one()
    # The completed job still says what version ACTUALLY ran (1), never the
    # schedule's current value (2).
    assert job.parameters["plan_version"] == 1
    assert job.parameters["plan"] == snapshot_plan


async def test_early_blocked_return_resolves_a_pre_created_jobs_row_unapproved_plan(db: AsyncSession, monkeypatch):
    """review finding, MAJOR: both REASON_BLOCKED guard blocks in
    run_schedule_now (plan not approved; no compiled plan) used to return
    without touching `existing_job_id`, leaving a pre-created row (item 6's
    claim-time insert, or Task 5's `POST .../run`) stuck at `pending`
    forever. This is the HITL-gate guard block."""
    tenant = await create_test_tenant(db, name="Blocked Pre-created Co")
    await set_tenant_context(db, str(tenant.id))

    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=None,
        cron_expression="0 6 * * 1",
        plan_status="pending_approval",  # never approved
    )

    pre_created = Job(
        tenant_id=tenant.id,
        job_type="scheduled_job",
        status="pending",
        parameters={"schedule_id": str(schedule.id), "use_pending": False},
    )
    db.add(pre_created)
    await db.flush()
    await db.commit()
    pre_created_id = pre_created.id

    outcome = await run_schedule_now(
        db, schedule.id, tenant_id=tenant.id, actor_id=None, use_pending=False, existing_job_id=pre_created_id
    )

    assert outcome.reason == REASON_BLOCKED
    assert outcome.jobs_row_id == pre_created_id

    job = (await db.execute(select(Job).where(Job.id == pre_created_id))).scalar_one()
    assert job.status == "completed"
    assert job.result_summary == {"reason": REASON_BLOCKED, "outputs": {}, "detail": "plan not approved"}
    assert job.error_message == "plan not approved"


async def test_early_blocked_return_resolves_a_pre_created_jobs_row_no_compiled_plan(db: AsyncSession, monkeypatch):
    """Same fix, the OTHER early REASON_BLOCKED guard block (no compiled plan
    to run) -- reached only past the HITL gate, so `plan_status="approved"`
    with an empty `plan_json` (e.g. an approved schedule whose plan was
    later cleared)."""
    tenant = await create_test_tenant(db, name="Blocked No Plan Co")
    await set_tenant_context(db, str(tenant.id))

    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json=None,
        next_run_at=None,
        cron_expression="0 6 * * 1",
        plan_status="approved",
        plan_version=1,
    )

    pre_created = Job(
        tenant_id=tenant.id,
        job_type="scheduled_job",
        status="pending",
        parameters={"schedule_id": str(schedule.id), "use_pending": False},
    )
    db.add(pre_created)
    await db.flush()
    await db.commit()
    pre_created_id = pre_created.id

    outcome = await run_schedule_now(
        db, schedule.id, tenant_id=tenant.id, actor_id=None, use_pending=False, existing_job_id=pre_created_id
    )

    assert outcome.reason == REASON_BLOCKED
    assert outcome.jobs_row_id == pre_created_id

    job = (await db.execute(select(Job).where(Job.id == pre_created_id))).scalar_one()
    assert job.status == "completed"
    assert job.result_summary == {"reason": REASON_BLOCKED, "outputs": {}, "detail": "no compiled plan to run"}
    assert job.error_message == "no compiled plan to run"


async def test_run_schedule_now_falls_back_to_inserting_when_existing_job_id_is_missing(db: AsyncSession, monkeypatch):
    """Defensive only (row deleted between enqueue and pickup) — must not crash."""
    tenant = await create_test_tenant(db, name="Missing Job Co")
    await set_tenant_context(db, str(tenant.id))

    async def fake_exec(ctx, params):
        return {"ok": True}

    monkeypatch.setitem(STEP_REGISTRY, "fake.step", _fake_spec("read", fake_exec))

    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "s1", "type": "fake.step", "params": {}}]},
        next_run_at=None,
        cron_expression="0 6 * * 1",
    )

    missing_id = uuid.uuid4()
    outcome = await run_schedule_now(
        db,
        schedule.id,
        tenant_id=tenant.id,
        actor_id=None,
        use_pending=False,
        existing_job_id=missing_id,
    )

    assert outcome.reason == REASON_DONE
    assert outcome.jobs_row_id != missing_id

    jobs = (await db.execute(select(Job).where(Job.tenant_id == tenant.id))).scalars().all()
    assert len(jobs) == 1
