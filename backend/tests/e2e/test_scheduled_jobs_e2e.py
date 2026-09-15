"""Scheduled Jobs — seeded-tenant executor e2e (Slice 2, Task 3, spec §B8):

    Seeded-tenant e2e: a due job runs exactly once, a missed one catches up
    once, a failed one pauses after the retry.

Drives the REAL `run_due_jobs` sweep against the real local Postgres test DB,
end to end, with FAKE step executors monkeypatched onto `STEP_REGISTRY` (the
run loop looks each step's type up in the registry at RUN time, never caching
it — see `app/workers/tasks/scheduled_jobs.py`'s module docstring) standing in
for BigQuery/report-compose/Drive so this test needs no live credentials while
still exercising every real choke point: the claim query, the `jobs` row, the
write-step audit-before-call, and the schedule bookkeeping the list/detail
page reads.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.database import set_tenant_context
from app.models.audit import AuditEvent
from app.models.job import Job
from app.models.pipeline import Schedule
from app.services.jobs.registry import STEP_REGISTRY, StepExecutionError, StepSpec
from app.workers.tasks.scheduled_jobs import REASON_DONE, REASON_ERROR, run_due_jobs
from tests.conftest import create_test_tenant

# The three-step shape spec §B7 describes for Inventory Aging Weekly: a source
# query, a compose-like step, then the one WRITE step (Drive delivery).
_PLAN_JSON = {
    "steps": [
        {"id": "s1_query", "type": "e2e.bigquery_sql", "params": {"query": "SELECT 1"}},
        {"id": "s2_compose", "type": "e2e.report_compose", "params": {"report_step": "s1_query"}},
        {
            "id": "s3_upload",
            "type": "e2e.drive_upload",
            "params": {"report_step": "s2_compose", "period_key": "2026-09-07"},
        },
    ]
}


def _fake_spec(kind: str, executor, idempotency=None, *, step_type: str) -> StepSpec:
    return StepSpec(
        type=step_type,
        label=f"E2E fake {step_type}",
        kind=kind,
        params_schema={"type": "object"},
        executor=executor,
        idempotency=idempotency,
    )


def _install_fake_steps(monkeypatch, call_log: list[str]):
    async def bigquery_sql(ctx, params):
        call_log.append("s1_query")
        return {"columns": ["n"], "rows": [[1]], "bytes_processed": 1024}

    async def report_compose(ctx, params):
        call_log.append("s2_compose")
        source = ctx.artifacts[params["report_step"]]
        return {"report_id": "fake-report-1", "title": "Fake Report", "row_count": len(source["rows"])}

    def drive_idem(ctx, params) -> str:
        return f"job:{ctx.job_id}:period:{params['period_key']}"

    async def drive_upload(ctx, params):
        call_log.append("s3_upload")
        report = ctx.artifacts[params["report_step"]]
        return {"pdf_url": f"https://drive.example/{report['report_id']}.pdf", "xlsx_url": None, "folder_id": "f1"}

    monkeypatch.setitem(
        STEP_REGISTRY, "e2e.bigquery_sql", _fake_spec("read", bigquery_sql, step_type="e2e.bigquery_sql")
    )
    monkeypatch.setitem(
        STEP_REGISTRY, "e2e.report_compose", _fake_spec("read", report_compose, step_type="e2e.report_compose")
    )
    monkeypatch.setitem(
        STEP_REGISTRY,
        "e2e.drive_upload",
        _fake_spec("write", drive_upload, idempotency=drive_idem, step_type="e2e.drive_upload"),
    )


async def test_a_due_job_runs_exactly_once_end_to_end(db, monkeypatch):
    tenant = await create_test_tenant(db, name="Scheduled Jobs E2E Co")
    await set_tenant_context(db, str(tenant.id))

    call_log: list[str] = []
    _install_fake_steps(monkeypatch, call_log)

    now = datetime.now(timezone.utc)
    schedule = Schedule(
        tenant_id=tenant.id,
        name="Inventory Aging Weekly",
        schedule_type="job",
        cron_expression="0 6 * * 1",
        timezone="America/Los_Angeles",
        is_active=True,
        instruction=(
            "Every Monday at 6am Pacific, compile the inventory aging report for "
            "Dimerco, Fedex and Panurgy and deliver the PDF and Excel workbook to "
            "the Reports / Inventory aging folder in Drive."
        ),
        plan_json=_PLAN_JSON,
        plan_version=1,
        plan_status="approved",
        catch_up="once",
        next_run_at=now - timedelta(minutes=5),
    )
    db.add(schedule)
    await db.flush()

    # First sweep tick: the job is due -> runs exactly once.
    stats = await run_due_jobs(db, tenant.id, now=now)
    assert stats["due"] == 1
    assert stats["ran"] == 1
    assert stats["failed"] == 0

    # Steps executed in plan order, exactly once each.
    assert call_log == ["s1_query", "s2_compose", "s3_upload"]

    jobs = (await db.execute(select(Job).where(Job.tenant_id == tenant.id))).scalars().all()
    assert len(jobs) == 1, "a due job must produce exactly one jobs row"
    job = jobs[0]
    assert job.job_type == "scheduled_job"
    assert job.status == "completed"
    assert job.result_summary["reason"] == REASON_DONE
    outputs = job.result_summary["outputs"]
    assert set(outputs) == {"s1_query", "s2_compose", "s3_upload"}
    assert outputs["s3_upload"]["pdf_url"].endswith(".pdf")
    assert job.parameters["schedule_id"] == str(schedule.id)
    assert job.parameters["plan_version"] == 1

    # Schedule bookkeeping the list/detail page reads.
    assert schedule.last_run_status == REASON_DONE
    assert schedule.last_run_at == now
    assert schedule.next_run_at is not None
    assert schedule.next_run_at > now

    # The WRITE step's idempotency-key audit event landed, before the call
    # (test_executor.py proves the ordering directly; this proves it end to end).
    started_events = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.tenant_id == tenant.id, AuditEvent.action == "e2e.drive_upload.started"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(started_events) == 1
    assert started_events[0].payload["idempotency_key"] == f"job:{schedule.id}:period:2026-09-07"

    # Second sweep tick, same instant: nothing else is due — no double run.
    stats2 = await run_due_jobs(db, tenant.id, now=now)
    assert stats2["due"] == 0
    jobs_after_second_tick = (await db.execute(select(Job).where(Job.tenant_id == tenant.id))).scalars().all()
    assert len(jobs_after_second_tick) == 1


async def test_a_missed_job_catches_up_exactly_once(db, monkeypatch):
    tenant = await create_test_tenant(db, name="Scheduled Jobs Catchup E2E Co")
    await set_tenant_context(db, str(tenant.id))

    call_log: list[str] = []
    _install_fake_steps(monkeypatch, call_log)

    now = datetime.now(timezone.utc)
    missed_due = now - timedelta(days=21)  # three missed weekly windows
    schedule = Schedule(
        tenant_id=tenant.id,
        name="Inventory Aging Weekly",
        schedule_type="job",
        cron_expression="0 6 * * 1",
        timezone="America/Los_Angeles",
        is_active=True,
        instruction="weekly inventory aging report",
        plan_json=_PLAN_JSON,
        plan_version=1,
        plan_status="approved",
        catch_up="once",
        next_run_at=missed_due,
    )
    db.add(schedule)
    await db.flush()

    stats = await run_due_jobs(db, tenant.id, now=now)
    assert stats["due"] == 1
    assert stats["ran"] == 1
    assert call_log == ["s1_query", "s2_compose", "s3_upload"]  # exactly one pass, not three

    jobs = (await db.execute(select(Job).where(Job.tenant_id == tenant.id))).scalars().all()
    assert len(jobs) == 1
    assert schedule.next_run_at > now


async def test_a_failed_job_pauses_after_the_one_retry(db, monkeypatch):
    tenant = await create_test_tenant(db, name="Scheduled Jobs Failure E2E Co")
    # Captured now: a step that raises with no db access of its own used to
    # make `_run_steps`'s `db.rollback()` a no-op (nothing had opened a
    # transaction since the last commit). Now that EVERY step unconditionally
    # re-sets tenant context first (the MAJOR tenant-context fix), that SET
    # LOCAL opens a real transaction, so the rollback is real and expires the
    # whole identity map -- `_finalize_run`'s own re-fetch keeps `schedule`/
    # `job` fresh, but nothing re-selects `Tenant`, so re-reading its expired
    # `.id` outside an awaited DB call raises MissingGreenlet.
    tenant_id = tenant.id
    await set_tenant_context(db, str(tenant_id))

    async def always_fails(ctx, params):
        raise StepExecutionError("e2e: the source query is unreachable")

    monkeypatch.setitem(
        STEP_REGISTRY, "e2e.bigquery_sql", _fake_spec("read", always_fails, step_type="e2e.bigquery_sql")
    )

    now = datetime.now(timezone.utc)
    schedule = Schedule(
        tenant_id=tenant_id,
        name="Inventory Aging Weekly",
        schedule_type="job",
        cron_expression="0 6 * * 1",
        timezone="America/Los_Angeles",
        is_active=True,
        instruction="weekly inventory aging report",
        plan_json={"steps": [_PLAN_JSON["steps"][0]]},
        plan_version=1,
        plan_status="approved",
        catch_up="once",
        next_run_at=now - timedelta(minutes=1),
    )
    db.add(schedule)
    await db.flush()

    await run_due_jobs(db, tenant_id, now=now)
    assert schedule.paused_at is None
    assert schedule.last_run_status == "retry_pending"
    retry_at = schedule.next_run_at

    await run_due_jobs(db, tenant_id, now=retry_at + timedelta(seconds=1))
    assert schedule.paused_at is not None
    assert schedule.last_run_status == "paused"

    jobs = (await db.execute(select(Job).where(Job.tenant_id == tenant_id))).scalars().all()
    assert len(jobs) == 2
    assert all(j.result_summary["reason"] == REASON_ERROR for j in jobs)

    pause_events = (
        (
            await db.execute(
                select(AuditEvent).where(AuditEvent.tenant_id == tenant_id, AuditEvent.action == "jobs.paused")
            )
        )
        .scalars()
        .all()
    )
    assert len(pause_events) == 1
