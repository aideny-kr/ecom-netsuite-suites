"""Audit log retention service — archives old events."""

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.audit import AuditEvent

logger = structlog.get_logger()


def get_retention_cutoff() -> datetime:
    """Return the cutoff date for audit retention."""
    return datetime.now(timezone.utc) - timedelta(days=settings.AUDIT_RETENTION_DAYS)


async def get_retention_stats(db: AsyncSession, tenant_id=None) -> dict:
    """Get audit table stats for monitoring."""
    cutoff = get_retention_cutoff()

    total_query = select(func.count()).select_from(AuditEvent)
    archivable_query = select(func.count()).select_from(AuditEvent).where(AuditEvent.timestamp < cutoff)

    if tenant_id:
        total_query = total_query.where(AuditEvent.tenant_id == tenant_id)
        archivable_query = archivable_query.where(AuditEvent.tenant_id == tenant_id)

    total = (await db.execute(total_query)).scalar() or 0
    archivable = (await db.execute(archivable_query)).scalar() or 0

    return {
        "total_events": total,
        "archivable_events": archivable,
        "retention_days": settings.AUDIT_RETENTION_DAYS,
        "cutoff_date": cutoff.isoformat(),
    }


def purge_old_events_sync(db: Session, batch_size: int = 5000) -> dict:
    """Delete audit events older than retention period. Sync version for Celery."""
    cutoff = get_retention_cutoff()

    total_deleted = 0
    while True:
        # Delete in batches to avoid long locks
        subq = select(AuditEvent.id).where(AuditEvent.timestamp < cutoff).limit(batch_size).subquery()
        result = db.execute(delete(AuditEvent).where(AuditEvent.id.in_(select(subq.c.id))))
        deleted = result.rowcount
        db.commit()
        total_deleted += deleted

        if deleted < batch_size:
            break

    logger.info(
        "audit.retention.purge_complete",
        total_deleted=total_deleted,
        cutoff=cutoff.isoformat(),
        retention_days=settings.AUDIT_RETENTION_DAYS,
    )

    return {
        "deleted": total_deleted,
        "cutoff_date": cutoff.isoformat(),
        "retention_days": settings.AUDIT_RETENTION_DAYS,
    }
