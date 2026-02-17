"""Celery task for audit log retention."""

from sqlalchemy.orm import Session

from app.services.audit_retention import purge_old_events_sync
from app.workers.base_task import sync_engine
from app.workers.celery_app import celery_app


@celery_app.task(name="tasks.audit_retention", queue="default")
def audit_retention_task():
    """Purge audit events older than the configured retention period."""
    with Session(sync_engine) as db:
        result = purge_old_events_sync(db)
    return result
