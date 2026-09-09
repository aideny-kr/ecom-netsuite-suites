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
from datetime import datetime, timedelta, timezone

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
    REASON_BUDGET,
    REASON_DONE,
    REASON_ERROR,
    RunOutcome,
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

    jobs_after_1 = (await db.execute(select(Job).where(Job.tenant_id == tenant_id))).scalars().all()
    assert len(jobs_after_1) == 1
    assert jobs_after_1[0].result_summary["reason"] == REASON_ERROR
    assert jobs_after_1[0].parameters["attempt"] == 1

    # Attempt 2, 15 minutes later: fails again -> pause + pause_reason + owner-notify audit.
    now2 = retry_at + timedelta(seconds=1)
    stats2 = await run_due_jobs(db, tenant_id, now=now2)
    assert stats2["ran"] == 1
    assert schedule.paused_at is not None
    assert schedule.pause_reason
    assert schedule.last_run_status == "paused"

    jobs_after_2 = (await db.execute(select(Job).where(Job.tenant_id == tenant_id))).scalars().all()
    assert len(jobs_after_2) == 2  # a SECOND jobs row for the retry (attempt=2)
    retry_job = next(j for j in jobs_after_2 if j.parameters.get("attempt") == 2)
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

    job = (await db.execute(select(Job).where(Job.tenant_id == tenant_id))).scalar_one()
    assert job.status == "failed"
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

    job = (await db.execute(select(Job).where(Job.tenant_id == tenant_id))).scalar_one()
    assert job.status == "failed"
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

    # The write step's audit-before-call commit must ALSO sit between two ctx
    # events within its own iteration (top-of-loop, then re-established after
    # the commit) -- this half already worked before the fix; kept here as a
    # regression guard against ever dropping it while fixing the read-step gap.
    s2_idx = events.index("exec:s2")
    s1_idx = events.index("exec:s1")
    between = events[s1_idx + 1 : s2_idx]
    assert "commit" in between, "the write step's audit-before-call commit must run inside its own iteration"
    commit_idx = between.index("commit")
    assert "ctx" in between[:commit_idx], "context must be set before the write step's audit log + commit"
    assert "ctx" in between[commit_idx:], "context must be re-set after the audit-before-call commit"


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
