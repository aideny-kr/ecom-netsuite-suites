import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent


async def log_event(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    category: str,
    action: str,
    actor_id: uuid.UUID | None = None,
    actor_type: str = "user",
    resource_type: str | None = None,
    resource_id: str | None = None,
    correlation_id: str | None = None,
    job_id: uuid.UUID | None = None,
    payload: dict | None = None,
    status: str = "success",
    error_message: str | None = None,
) -> AuditEvent:
    """Append an audit event. This is insert-only — no updates or deletes."""
    event = AuditEvent(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_type=actor_type,
        category=category,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        correlation_id=correlation_id,
        job_id=job_id,
        payload=payload,
        status=status,
        error_message=error_message,
    )
    db.add(event)
    await db.flush()
    return event
