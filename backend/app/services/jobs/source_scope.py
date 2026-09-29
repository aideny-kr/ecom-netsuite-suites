"""Task-local guard at the actual provider selection, including report subreads.

Legacy/manual calls have no scope. A modern workflow installs one for its step;
every supported provider checks the selected ORM record before using credentials.
Failures survive wrappers that normally degrade optional comparison errors.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy import select

from app.core.database import set_tenant_context
from app.models.job import Job
from app.models.pipeline import Schedule
from app.services.jobs.inspection import plan_fingerprint
from app.services.jobs.readiness import digest, execution_fingerprint, inspect_readiness, source_binding


class SourceScopeChangedError(RuntimeError):
    pass


@dataclass
class SourceScope:
    ctx: object
    version: int
    readiness_hash: str
    error: str | None = None


_scope: ContextVar[SourceScope | None] = ContextVar("workflow_source_scope", default=None)


@contextmanager
def reviewed_sources(ctx, version, readiness_hash):
    token = _scope.set(SourceScope(ctx, version, readiness_hash) if readiness_hash else None)
    try:
        yield
        raise_if_source_changed()
    finally:
        _scope.reset(token)


def raise_if_source_changed():
    scope = _scope.get()
    if scope is not None and scope.error:
        raise SourceScopeChangedError(scope.error)


async def check_selected_source(db, tenant_id, provider, selected):
    scope = _scope.get()
    if scope is None:
        return
    raise_if_source_changed()
    scope.error = "Workflow source or authorization changed; validate and approve again."
    ctx = scope.ctx
    if str(tenant_id) != str(ctx.tenant_id) or selected is None:
        raise_if_source_changed()
    await set_tenant_context(db, str(ctx.tenant_id))
    schedule = await db.scalar(
        select(Schedule)
        .where(Schedule.id == ctx.job_id, Schedule.tenant_id == ctx.tenant_id)
        .execution_options(populate_existing=True)
    )
    job = await db.scalar(
        select(Job)
        .where(Job.id == ctx.run_id, Job.tenant_id == ctx.tenant_id)
        .execution_options(populate_existing=True)
    )
    if (
        schedule is None
        or job is None
        or job.status != "running"
        or schedule.paused_at is not None
        or not schedule.is_active
        or schedule.plan_version != scope.version
    ):
        raise_if_source_changed()
    params = job.parameters or {}
    pending = ctx.execution_mode == "test" and params.get("use_pending", False)
    if ctx.execution_mode == "test":
        if params.get("plan_hash") != plan_fingerprint(schedule, use_pending=pending):
            raise_if_source_changed()
    elif ((schedule.parameters or {}).get("workflow_review") or {}).get("execution_hash") != execution_fingerprint(
        schedule
    ):
        raise_if_source_changed()
    review = await inspect_readiness(db, schedule, use_pending=pending)
    if not review["ready"] or review["readiness_hash"] != scope.readiness_hash:
        raise_if_source_changed()
    # inspect_readiness refreshes source records; compare the actual selected
    # record with the reviewed eligibility and credential/account fingerprint.
    binding = source_binding(selected)
    if (
        str(selected.tenant_id) != str(ctx.tenant_id)
        or binding["provider"] != provider
        or binding["status"] != "active"
        or not binding["enabled"]
        or review["source_binding_hashes"].get(binding["id"]) != digest(binding)
    ):
        raise_if_source_changed()
    scope.error = None
