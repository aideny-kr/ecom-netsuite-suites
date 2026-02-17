import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from celery import Task
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.audit import AuditEvent
from app.models.job import Job

sync_engine = create_engine(settings.DATABASE_URL_SYNC)


@contextmanager
def tenant_session(tenant_id: str):
    """Create a sync DB session with RLS tenant context set.

    Ensures SET LOCAL app.current_tenant_id is called within the transaction
    so all queries are scoped to the given tenant.
    """
    with Session(sync_engine) as session:
        session.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_id}'"))
        yield session


class InstrumentedTask(Task):
    """Base task that auto-creates a Job record and emits audit events."""

    abstract = True
    _job_id: uuid.UUID | None = None
    _correlation_id: str | None = None

    def before_start(self, task_id, args, kwargs):
        self._correlation_id = kwargs.pop("correlation_id", None) or str(uuid.uuid4())
        tenant_id = kwargs.get("tenant_id")
        if not tenant_id:
            return

        with tenant_session(tenant_id) as session:
            job = Job(
                tenant_id=tenant_id,
                job_type=self.name,
                status="running",
                correlation_id=self._correlation_id,
                connection_id=kwargs.get("connection_id"),
                started_at=datetime.now(timezone.utc),
                parameters=kwargs,
                celery_task_id=task_id,
            )
            session.add(job)
            session.flush()
            self._job_id = job.id

            session.add(
                AuditEvent(
                    tenant_id=tenant_id,
                    category="job",
                    action="job.start",
                    actor_type="system",
                    resource_type="job",
                    resource_id=str(job.id),
                    correlation_id=self._correlation_id,
                    job_id=job.id,
                    payload={"task_name": self.name},
                )
            )
            session.commit()

    def on_success(self, retval, task_id, args, kwargs):
        tenant_id = kwargs.get("tenant_id")
        if not tenant_id or not self._job_id:
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
        tenant_id = kwargs.get("tenant_id")
        if not tenant_id or not self._job_id:
            return

        with tenant_session(tenant_id) as session:
            job = session.get(Job, self._job_id)
            if job:
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
