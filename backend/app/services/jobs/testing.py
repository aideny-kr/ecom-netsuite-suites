"""Bounded new-report tests, using the real scheduled-step worker. No activation."""

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models.job import Job
from app.services import audit_service
from app.services.jobs.inspection import plan_fingerprint
from app.services.jobs.readiness import TEST_SECONDS, inspect_readiness, test_blockers
from app.services.jobs.registry import StepContext


async def enqueue_test(db, schedule, actor_id, body):
    if schedule.schedule_type != "job" or schedule.owner_id != actor_id:
        raise ValueError("Only the workflow owner can test a compiled workflow.")
    if schedule.paused_at is not None or not schedule.is_active:
        raise ValueError("Workflow is stopped. Resume before testing.")
    review = await inspect_readiness(db, schedule, use_pending=body.use_pending)
    if review["plan_hash"] != body.expected_plan_hash or review["readiness_hash"] != body.readiness_hash:
        raise ValueError("Plan, permissions, sources or policy changed. Validate again.")
    if not review["ready"] or not review["test_supported"]:
        raise ValueError("; ".join(review["blockers"] + review["readiness_blockers"] + review["test_blockers"]))
    seconds = min(TEST_SECONDS, (schedule.budget_json or {}).get("seconds") or TEST_SECONDS)
    job = Job(
        tenant_id=schedule.tenant_id,
        job_type="scheduled_job",
        status="pending",
        parameters={
            "schedule_id": str(schedule.id),
            "execution_mode": "test",
            "plan": schedule.pending_plan_json if body.use_pending else schedule.plan_json,
            "plan_hash": review["plan_hash"],
            "readiness_hash": review["readiness_hash"],
            "plan_version": review["plan_version"],
            "use_pending": body.use_pending,
            "actor_id": str(actor_id),
            "actor_type": "user",
            "budget": {"seconds": seconds},
            "control_version": schedule.plan_version,
            "recovery_version": 1,
            "dispatch_ready": True,
            "due_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    db.add(job)
    await db.flush()
    await audit_service.log_event(
        db,
        tenant_id=schedule.tenant_id,
        category="jobs",
        action="jobs.test.enqueued",
        actor_id=actor_id,
        resource_type="schedule",
        resource_id=str(schedule.id),
        job_id=job.id,
        payload={"plan_hash": review["plan_hash"], "readiness_hash": review["readiness_hash"]},
    )
    return job


async def test_guard(db, schedule, job):
    params = job.parameters or {}
    if params.get("execution_mode") != "test" or params.get("actor_id") != str(schedule.owner_id):
        return "Test owner or execution mode changed."
    if schedule.paused_at is not None or not schedule.is_active:
        return "Workflow is stopped."
    if params.get("plan_hash") != plan_fingerprint(schedule, use_pending=params.get("use_pending", False)):
        return "Workflow changed since this test was requested."
    plan = schedule.pending_plan_json if params.get("use_pending") else schedule.plan_json
    if params.get("plan") != plan or test_blockers(plan):
        return "Test plan is outside the supported envelope."
    review = await inspect_readiness(db, schedule, use_pending=params.get("use_pending", False))
    if not review["ready"] or review["readiness_hash"] != params.get("readiness_hash"):
        return "Source, permission or policy readiness changed. Validate again."
    return None


async def run_test(db, schedule, job):
    """Caller holds the existing schedule advisory lock. A started test never replays."""
    from app.workers.tasks.scheduled_jobs import RunOutcome, _run_steps

    params = dict(job.parameters or {})
    job_id, tenant_id, schedule_id = job.id, schedule.tenant_id, schedule.id
    if job.status not in {"pending", "running"}:
        summary = job.result_summary or {}
        return RunOutcome(summary.get("reason", "blocked"), job_id, summary.get("outputs", {}))
    error = (
        "Test was interrupted; inspect retained outputs and explicitly request a new test."
        if job.status == "running"
        else await test_guard(db, schedule, job)
    )
    if error:
        from app.models.report import Report

        retained = (
            await db.scalars(select(Report.id).where(Report.tenant_id == tenant_id, Report.source_run_id == job_id))
        ).all()
        job.status = "completed"
        job.completed_at = datetime.now(timezone.utc)
        job.result_summary = {
            **(job.result_summary or {}),
            "reason": "blocked",
            "detail": error,
            "verification": "not_verified",
            "retained_report_ids": [str(rid) for rid in retained],
            "outputs": {
                **((job.result_summary or {}).get("outputs") or {}),
                **{f"retained_{i}": {"report_id": str(rid)} for i, rid in enumerate(retained)},
            },
        }
        await audit_service.log_event(
            db,
            tenant_id=tenant_id,
            category="jobs",
            action="jobs.test.blocked",
            resource_type="job",
            resource_id=str(job_id),
            job_id=job_id,
            payload={"reason": error},
            status="error",
        )
        await db.commit()
        return RunOutcome("blocked", job_id, job.result_summary["outputs"])
    actor_id = uuid.UUID(params["actor_id"])
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    job.correlation_id = str(uuid.uuid4())
    correlation_id = job.correlation_id
    period_key = datetime.fromisoformat(params["due_at"]).astimezone(ZoneInfo(schedule.timezone)).date().isoformat()
    await audit_service.log_event(
        db,
        tenant_id=tenant_id,
        category="jobs",
        action="jobs.test.start",
        actor_id=actor_id,
        resource_type="job",
        resource_id=str(job_id),
        job_id=job_id,
        correlation_id=correlation_id,
    )
    await db.commit()
    ctx = StepContext(
        job_id=schedule_id,
        run_id=job_id,
        tenant_id=tenant_id,
        db=db,
        budget=params["budget"],
        actor_id=actor_id,
        actor_type="user",
        period_key=period_key,
        execution_mode="test",
    )
    reason, outputs, detail = await _run_steps(
        db,
        ctx=ctx,
        steps=params["plan"]["steps"],
        budget=params["budget"],
        correlation_id=correlation_id,
        job_id=job_id,
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_type="user",
        control_version=params["control_version"],
    )
    from app.core.database import set_tenant_context

    await set_tenant_context(db, str(tenant_id))
    job = await db.scalar(
        select(Job).where(Job.id == job_id, Job.tenant_id == tenant_id).execution_options(populate_existing=True)
    )
    if job.status != "cancelled":
        job.status = "completed" if reason != "error" else "failed"
    job.completed_at = datetime.now(timezone.utc)
    job.result_summary = {
        **(job.result_summary or {}),
        "reason": reason,
        "outputs": outputs,
        "detail": detail,
        "verification": "not_verified",
        "execution_mode": "test",
    }
    await audit_service.log_event(
        db,
        tenant_id=tenant_id,
        category="jobs",
        action="jobs.test.finish",
        actor_id=actor_id,
        resource_type="job",
        resource_id=str(job_id),
        job_id=job_id,
        correlation_id=correlation_id,
        payload={"reason": reason},
    )
    await db.commit()
    return RunOutcome(reason, job_id, outputs)
