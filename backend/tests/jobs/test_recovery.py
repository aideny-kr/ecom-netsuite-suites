"""Durable scheduler boundaries; all effects are synthetic, against real Postgres."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.job import Job
from app.models.pipeline import Schedule
from app.services.jobs.registry import STEP_REGISTRY
from app.workers.tasks import scheduled_jobs as jobs
from tests.conftest import create_test_tenant
from tests.jobs.test_executor import _fake_spec, _seed_job_schedule


async def seed(db, monkeypatch, executor, *, kind="read", budget=None):
    tenant = await create_test_tenant(db)
    monkeypatch.setitem(
        STEP_REGISTRY,
        "fake.step",
        _fake_spec(kind, executor, (lambda c, p: f"{c.job_id}:{c.period_key}") if kind == "write" else None),
    )
    schedule = await _seed_job_schedule(
        db,
        tenant,
        plan_json={"steps": [{"id": "one", "type": "fake.step", "params": {}}]},
        next_run_at=None,
        budget_json=budget,
    )
    await db.commit()
    return tenant.id, schedule.id


async def test_completed_broker_redelivery_does_not_repeat_effect(db, monkeypatch):
    calls = []

    async def execute(ctx, params):
        calls.append(ctx.run_id)
        return {"ok": True}

    tid, sid = await seed(db, monkeypatch, execute, kind="write")
    first = await jobs.run_schedule_now(db, sid, tenant_id=tid, actor_id=None)
    repeated = await jobs.run_schedule_now(db, sid, tenant_id=tid, actor_id=None, existing_job_id=first.jobs_row_id)
    assert calls == [first.jobs_row_id]
    assert repeated == first


@pytest.mark.parametrize("status", ["cancelled", "failed", "running"])
async def test_non_pending_delivery_never_reenters_executor(db, monkeypatch, status):
    calls = []

    async def execute(ctx, params):
        calls.append(1)
        return {"ok": True}

    tid, sid = await seed(db, monkeypatch, execute)
    pending = Job(tenant_id=tid, job_type="scheduled_job", status=status, parameters={"schedule_id": str(sid)})
    db.add(pending)
    await db.commit()
    jid = pending.id
    outcome = await jobs.run_schedule_now(db, sid, tenant_id=tid, actor_id=None, existing_job_id=jid)
    assert not calls
    assert outcome.reason != jobs.REASON_DONE


@pytest.mark.parametrize("mismatch", ["missing", "tenant", "schedule", "type"])
async def test_job_identity_is_not_replaceable_or_borrowable(db, monkeypatch, mismatch):
    calls = []

    async def execute(ctx, params):
        calls.append(1)
        return {"ok": True}

    tid, sid = await seed(db, monkeypatch, execute)
    jid = uuid.uuid4()
    if mismatch != "missing":
        job = Job(
            id=jid,
            tenant_id=uuid.uuid4() if mismatch == "tenant" else tid,
            job_type="other" if mismatch == "type" else "scheduled_job",
            status="pending",
            parameters={"schedule_id": str(uuid.uuid4() if mismatch == "schedule" else sid)},
        )
        db.add(job)
        await db.commit()
    result = await jobs.run_schedule_now(db, sid, tenant_id=tid, actor_id=None, existing_job_id=jid)
    assert result.reason == jobs.REASON_BLOCKED
    assert not calls


@pytest.mark.parametrize("stop", ["pause", "disable", "cancel", "version"])
async def test_stop_is_checked_between_steps(db, monkeypatch, stop):
    calls = []

    async def execute(ctx, params):
        calls.append(ctx.current_step_id)
        schedule = await db.get(Schedule, ctx.job_id)
        if stop == "pause":
            schedule.paused_at = datetime.now(timezone.utc)
        if stop == "disable":
            schedule.is_active = False
        if stop == "cancel":
            (await db.get(Job, ctx.run_id)).status = "cancelled"
        if stop == "version":
            schedule.plan_version += 1
        return {"ok": True}

    tid, sid = await seed(db, monkeypatch, execute)
    schedule = await db.get(Schedule, sid)
    schedule.plan_json = {"steps": [{"id": name, "type": "fake.step", "params": {}} for name in ["one", "two"]]}
    await db.commit()
    result = await jobs.run_schedule_now(db, sid, tenant_id=tid, actor_id=None)
    assert calls == ["one"]
    assert result.reason == jobs.REASON_BLOCKED
    if stop == "cancel":
        assert (await db.get(Job, result.jobs_row_id)).status == "cancelled"


async def test_zero_budget_prevents_first_call(db, monkeypatch):
    calls = []

    async def execute(ctx, params):
        calls.append(1)
        return {"ok": True}

    tid, sid = await seed(db, monkeypatch, execute, budget={"seconds": 0})
    result = await jobs.run_schedule_now(db, sid, tenant_id=tid, actor_id=None)
    assert result.reason == jobs.REASON_BUDGET
    assert not calls


async def test_unknown_write_is_not_retried_or_reported_as_success(db, monkeypatch):
    calls = []

    async def execute(ctx, params):
        calls.append(1)  # remote accepted, local response lost
        raise TimeoutError("synthetic receipt loss")

    tid, sid = await seed(db, monkeypatch, execute, kind="write")
    result = await jobs.run_schedule_now(db, sid, tenant_id=tid, actor_id=None, retry_on_error=True)
    row = await db.get(Schedule, sid)
    assert result.reason == jobs.REASON_BLOCKED
    assert row.retry_job_id is None
    assert row.paused_at is not None
    assert (await db.get(Job, result.jobs_row_id)).result_summary["verification"] == "uncertain"
    # Even an operator clearing pause cannot silently replay an unknown effect.
    row.paused_at = None
    await db.commit()
    second = await jobs.run_schedule_now(db, sid, tenant_id=tid, actor_id=None)
    assert second.reason == jobs.REASON_BLOCKED
    assert calls == [1]


async def test_retry_keeps_exact_original_plan_and_budget(db, monkeypatch):
    async def execute(ctx, params):
        raise RuntimeError("synthetic pre-effect read failure")

    tid, sid = await seed(db, monkeypatch, execute, budget={"seconds": 9})
    first = await jobs.run_schedule_now(db, sid, tenant_id=tid, actor_id=None, retry_on_error=True)
    schedule = await db.get(Schedule, sid)
    retry = await db.get(Job, schedule.retry_job_id)
    assert retry.parameters["plan"] == schedule.plan_json
    assert retry.parameters["plan_version"] == schedule.plan_version
    assert retry.parameters["budget"] == {"seconds": 9}
    assert retry.parameters["operation_id"] == str(first.jobs_row_id)


async def test_lost_dispatch_is_recovered_with_saved_identity(db, monkeypatch):
    from app.services.schedule_service import enqueue_run

    calls = []

    async def execute(ctx, params):
        calls.append((ctx.actor_type, ctx.actor_id))
        return {"ok": True}

    tid, sid = await seed(db, monkeypatch, execute)
    schedule = await db.get(Schedule, sid)
    # A synthetic principal reference: generic test step has no provider access.
    actor = uuid.uuid4()
    pending = await enqueue_run(db, schedule=schedule, tenant_id=tid, actor_id=actor, use_pending=False)
    pending.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    await db.commit()
    jid = pending.id
    await jobs.run_due_jobs(db, tid)
    assert calls == [("user", actor)]
    assert (await db.get(Job, jid)).result_summary["reason"] == jobs.REASON_DONE
    await jobs.run_due_jobs(db, tid)
    assert len(calls) == 1


async def test_interrupted_read_fails_without_permanently_fencing_future_work(db, monkeypatch):
    calls = []

    async def execute(ctx, params):
        calls.append(1)
        return {"ok": True}

    tid, sid = await seed(db, monkeypatch, execute)
    dead = Job(
        tenant_id=tid,
        job_type="scheduled_job",
        status="running",
        parameters={"schedule_id": str(sid), "recovery_version": 1, "dispatch_ready": True},
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db.add(dead)
    await db.commit()
    jid = dead.id
    await jobs.run_due_jobs(db, tid)
    assert not calls
    assert (await db.get(Job, jid)).result_summary["reason"] == jobs.REASON_ERROR
    assert (await db.get(Schedule, sid)).paused_at is None
    result = await jobs.run_schedule_now(db, sid, tenant_id=tid, actor_id=None)
    assert result.reason == jobs.REASON_DONE
    assert calls == [1]


async def test_completed_execution_receipt_survives_finalizer_interruption(db, monkeypatch):
    calls = []

    async def execute(ctx, params):
        calls.append(1)
        return {"ok": True}

    tid, sid = await seed(db, monkeypatch, execute, kind="write")
    receipt = {"execution_complete": True, "reason": "done", "outputs": {"one": {"ok": True}}}
    pending = Job(
        tenant_id=tid,
        job_type="scheduled_job",
        status="running",
        parameters={"schedule_id": str(sid)},
        result_summary=receipt,
    )
    db.add(pending)
    await db.commit()
    result = await jobs.run_schedule_now(db, sid, tenant_id=tid, actor_id=None, existing_job_id=pending.id)
    assert result.reason == "done"
    assert result.outputs == receipt["outputs"]
    assert not calls
    assert pending.status == "completed"


async def test_run_deadline_stops_a_blocked_read(db, monkeypatch):
    import asyncio

    async def execute(ctx, params):
        await asyncio.sleep(5)
        return {"ok": True}

    tid, sid = await seed(db, monkeypatch, execute, budget={"seconds": 0.02})
    result = await asyncio.wait_for(jobs.run_schedule_now(db, sid, tenant_id=tid, actor_id=None), 1)
    assert result.reason == jobs.REASON_BUDGET


async def test_overlap_does_not_accumulate_occurrences_behind_an_active_run(db, monkeypatch):
    async def execute(ctx, params):
        return {"ok": True}

    tid, sid = await seed(db, monkeypatch, execute)
    schedule = await db.get(Schedule, sid)
    now = datetime.now(timezone.utc)
    schedule.next_run_at = now - timedelta(minutes=5)
    schedule.cron_expression = "* * * * *"
    active = Job(tenant_id=tid, job_type="scheduled_job", status="running", parameters={"schedule_id": str(sid)})
    db.add(active)
    await db.commit()
    claims = await jobs._claim_due_schedules(db, tid, now)
    assert claims == []
    assert schedule.next_run_at < now  # remains due for one catch-up after owner finishes


async def test_deleted_schedule_does_not_poison_tenant_recovery(db, monkeypatch):
    calls = []

    async def execute(ctx, params):
        calls.append(1)
        return {"ok": True}

    tid, sid = await seed(db, monkeypatch, execute)
    orphan = Job(
        tenant_id=tid,
        job_type="scheduled_job",
        status="pending",
        parameters={"schedule_id": str(uuid.uuid4()), "dispatch_ready": True},
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db.add(orphan)
    schedule = await db.get(Schedule, sid)
    schedule.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.commit()
    jid = orphan.id
    await jobs.run_due_jobs(db, tid)
    assert calls == [1]
    assert (await db.get(Job, jid)).result_summary["reason"] == jobs.REASON_BLOCKED


async def test_finalizer_double_failure_preserves_effect_fence(db, monkeypatch):
    calls = []

    async def execute(ctx, params):
        calls.append(1)
        raise RuntimeError("remote accepted before synthetic transport error")

    tid, sid = await seed(db, monkeypatch, execute, kind="write")

    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic finalizer failure")

    monkeypatch.setattr(jobs, "_finalize_run", fail)
    first = await jobs.run_schedule_now(db, sid, tenant_id=tid, actor_id=None)
    await db.refresh(await db.get(Job, first.jobs_row_id))
    assert (await db.get(Job, first.jobs_row_id)).result_summary["verification"] == "uncertain"
    second = await jobs.run_schedule_now(db, sid, tenant_id=tid, actor_id=None)
    assert second.reason == jobs.REASON_BLOCKED
    assert calls == [1]


async def test_new_request_settles_prior_success_receipt_instead_of_marking_unknown(db, monkeypatch):
    async def execute(ctx, params):
        return {"ok": True}

    tid, sid = await seed(db, monkeypatch, execute)
    completed = Job(
        tenant_id=tid,
        job_type="scheduled_job",
        status="running",
        parameters={"schedule_id": str(sid)},
        result_summary={"execution_complete": True, "reason": "done", "outputs": {}},
    )
    db.add(completed)
    await db.commit()
    jid = completed.id
    outcome = await jobs.run_schedule_now(db, sid, tenant_id=tid, actor_id=None)
    assert outcome.reason == "done"
    previous = await db.get(Job, jid)
    assert previous.status == "completed"
    assert previous.result_summary.get("verification") != "uncertain"


async def test_nullable_budget_survives_retry_without_losing_bounded_limits(db, monkeypatch):
    async def execute(ctx, params):
        raise RuntimeError("synthetic pre-effect read failure")

    tid, sid = await seed(db, monkeypatch, execute, budget={"seconds": None, "bytes_scanned": 10, "usd": None})
    first = await jobs.run_schedule_now(db, sid, tenant_id=tid, actor_id=None, retry_on_error=True)
    assert first.reason == jobs.REASON_ERROR
    schedule = await db.get(Schedule, sid)
    retry = await db.get(Job, schedule.retry_job_id)
    assert retry.parameters["remaining_budget"] == {"seconds": None, "bytes_scanned": 10, "usd": None}


async def test_legacy_pending_dispatch_is_retired_without_replay_or_blocking_new_work(db, monkeypatch):
    calls = []

    async def execute(ctx, params):
        calls.append(ctx.run_id)
        return {"ok": True}

    tid, sid = await seed(db, monkeypatch, execute)
    schedule = await db.get(Schedule, sid)
    schedule.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    legacy = Job(
        tenant_id=tid,
        job_type="scheduled_job",
        status="pending",
        parameters={"schedule_id": str(sid)},
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db.add(legacy)
    await db.commit()
    legacy_id = legacy.id
    result = await jobs.run_due_jobs(db, tid)
    assert result["ran"] == 1
    assert len(calls) == 1
    assert legacy_id not in calls
    retired = await db.get(Job, legacy_id)
    assert retired.status == "completed"
    assert retired.result_summary["reason"] == "blocked"
    await jobs.run_schedule_now(db, sid, tenant_id=tid, actor_id=None, existing_job_id=legacy_id)
    assert len(calls) == 1
