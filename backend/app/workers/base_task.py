import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from celery import Task
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import pool_health_kwargs
from app.models.audit import AuditEvent
from app.models.job import Job

# Without pre-ping, a connection the server closed while it sat idle in the pool failed
# the next task's before_start, so the task never ran and no jobs row recorded it
# (staging 2026-10-26..28).
sync_engine = create_engine(settings.DATABASE_URL_SYNC, **pool_health_kwargs(settings.DATABASE_URL_SYNC))


def set_tenant_context_sync(session: Session, tenant_id: str) -> None:
    """Set RLS tenant context (SET LOCAL, scoped to the current transaction) on a
    sync Session. Sync twin of ``app.core.database.set_tenant_context``.

    SET LOCAL doesn't support $1 bind params in PostgreSQL, so the tenant_id is
    validated as a UUID first to prevent SQL injection via the f-string.

    Must be re-run after every commit within a long-running sync task — SET
    LOCAL is cleared when its transaction ends, silently dropping tenant scoping
    for whatever runs next in the same session (see
    ``stripe_sync.refresh_payout_statuses`` for a caller that re-establishes it
    after mid-run commits).
    """
    validated = str(uuid.UUID(str(tenant_id)))
    session.execute(text(f"SET LOCAL app.current_tenant_id = '{validated}'"))


@contextmanager
def tenant_session(tenant_id: str):
    """Create a sync DB session with RLS tenant context set.

    Ensures SET LOCAL app.current_tenant_id is called within the transaction
    so all queries are scoped to the given tenant.
    """
    with Session(sync_engine) as session:
        set_tenant_context_sync(session, tenant_id)
        yield session


class InstrumentedTask(Task):
    """Base task that auto-creates a Job record and emits audit events."""

    abstract = True
    _job_id: uuid.UUID | None = None
    _correlation_id: str | None = None

    # System-wide tasks (no tenant) use this sentinel ID for job tracking
    SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"

    def before_start(self, task_id, args, kwargs):
        # This run's job id is chosen here, before any database work. The task object is
        # shared by every run in this worker process (and Celery retries reuse task_id), so
        # this is the only identity that names this run and nothing else. If this start
        # fails, on_failure writes this run's row under the same id, whether or not this
        # commit landed, and never touches another run's row.
        self._job_id = uuid.uuid4()
        self._correlation_id = kwargs.pop("correlation_id", None) or str(uuid.uuid4())
        tenant_id = kwargs.get("tenant_id") or self.SYSTEM_TENANT_ID

        with tenant_session(tenant_id) as session:
            session.add(self._job_row(tenant_id, task_id, kwargs, status="running"))
            session.add(
                AuditEvent(
                    tenant_id=tenant_id,
                    category="job",
                    action="job.start",
                    actor_type="system",
                    resource_type="job",
                    resource_id=str(self._job_id),
                    correlation_id=self._correlation_id,
                    job_id=self._job_id,
                    payload={"task_name": self.name},
                )
            )
            session.commit()

    def on_success(self, retval, task_id, args, kwargs):
        tenant_id = kwargs.get("tenant_id") or self.SYSTEM_TENANT_ID
        if not self._job_id:
            return

        with tenant_session(tenant_id) as session:
            job = session.get(Job, self._job_id)
            if job:
                job.status = "completed"
                job.completed_at = datetime.now(timezone.utc)
                job.result_summary = retval if isinstance(retval, dict) else {"result": str(retval)}

            session.add(
                AuditEvent(
                    tenant_id=tenant_id,
                    category="job",
                    action="job.complete",
                    actor_type="system",
                    resource_type="job",
                    resource_id=str(self._job_id),
                    correlation_id=self._correlation_id,
                    job_id=self._job_id,
                    status="success",
                )
            )
            session.commit()

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        tenant_id = kwargs.get("tenant_id") or self.SYSTEM_TENANT_ID
        if not self._job_id:
            return
        # Best effort: whatever failed the task may break this write too, and it must not
        # replace the task's own error.
        try:
            self._record_failure(tenant_id, task_id, kwargs, exc)
        except Exception as record_exc:
            print(f"[InstrumentedTask] {self.name}[{task_id}] failure not recorded: {record_exc!r}", flush=True)

    def _job_row(self, tenant_id, task_id, kwargs, *, status: str) -> Job:
        return Job(
            id=self._job_id,
            tenant_id=tenant_id,
            job_type=self.name,
            status=status,
            correlation_id=self._correlation_id,
            connection_id=kwargs.get("connection_id"),
            started_at=datetime.now(timezone.utc),
            parameters=kwargs,
            celery_task_id=task_id,
        )

    def _record_failure(self, tenant_id, task_id, kwargs, exc) -> None:
        with tenant_session(tenant_id) as session:
            job = session.get(Job, self._job_id)
            if job is None:
                # before_start's commit did not land, so this run has no row yet.
                job = self._job_row(tenant_id, task_id, kwargs, status="failed")
                session.add(job)
            job.status = "failed"
            job.completed_at = datetime.now(timezone.utc)
            job.error_message = str(exc)

            session.add(
                AuditEvent(
                    tenant_id=tenant_id,
                    category="job",
                    action="job.failed",
                    actor_type="system",
                    resource_type="job",
                    resource_id=str(self._job_id),
                    correlation_id=self._correlation_id,
                    job_id=self._job_id,
                    status="error",
                    error_message=str(exc),
                )
            )
            session.commit()
