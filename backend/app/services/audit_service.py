import json
import uuid
from datetime import date, datetime
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent


def _exact(value: object) -> str:
    """Exact text for the values money and record evidence carry; never a float."""
    if isinstance(value, Decimal | uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


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
    """Append an audit event. This is insert-only — no updates or deletes.

    The payload is converted to JSON here, before it joins the caller's transaction: a value the
    column could not store (a Decimal in a refusal's detail, staging 2026-10-18) used to fail at
    flush, leaving the session unusable and ending the caller's whole request.
    """
    if payload is not None:
        payload = json.loads(json.dumps(payload, default=_exact))
    if correlation_id is None:
        ctx = structlog.contextvars.get_contextvars()
        correlation_id = ctx.get("correlation_id")

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
