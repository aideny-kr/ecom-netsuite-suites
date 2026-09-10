"""Shared manual/daily Solidus refresh reservation, using existing jobs and audit."""

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select

from app.core.database import set_tenant_context
from app.models.audit import AuditEvent
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.models.tenant import Tenant
from app.models.transaction_ops import TransactionConfig
from app.services import audit_service
from app.services.ingestion.sync_status import solidus_sync_status
from app.workers.celery_app import celery_app


def publish_refresh(tenant_id, connection_id, task_id):
    with celery_app.connection_for_write(
        connect_timeout=1,
        transport_options={
            "socket_connect_timeout": 1,
            "socket_timeout": 1,
            "retry_on_timeout": False,
            "max_retries": 0,
        },
    ) as broker:
        celery_app.send_task(
            "tasks.solidus_sync",
            task_id=task_id,
            queue="sync",
            retry=False,
            connection=broker,
            kwargs={
                "tenant_id": str(tenant_id),
                "connection_id": str(connection_id),
                "refresh_request_id": task_id,
                "correlation_id": task_id,
            },
        )


async def queue_refresh(db, tenant_id, connection_id, *, actor_id=None, daily=False, now=None):
    now = now or datetime.now(timezone.utc)
    await set_tenant_context(db, tenant_id)
    connection = await db.scalar(
        select(Connection)
        .join(Tenant, Tenant.id == Connection.tenant_id)
        .where(
            Connection.id == connection_id,
            Connection.tenant_id == tenant_id,
            Connection.provider == "solidus",
            Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
            Tenant.is_active.is_(True),
            Connection.metadata_json["api_profile"].astext == "framework_sync",
        )
        .with_for_update(of=Connection, skip_locked=daily)
    )
    if connection is None:
        await db.commit()
        return {"status": "unavailable"}
    current = await solidus_sync_status(db, tenant_id, connection_id)
    if current["status"] in {"queued", "running"}:
        await db.commit()
        return {"job_id": current["job_id"], "status": current["status"], "already_running": True}
    if daily:
        completed = current.get("last_completed_at")
        if completed and now - datetime.fromisoformat(completed) < timedelta(days=1):
            await db.commit()
            return {"status": "fresh"}
        attempts = (
            await db.scalars(
                select(AuditEvent.timestamp)
                .where(
                    AuditEvent.tenant_id == tenant_id,
                    AuditEvent.category == "sync",
                    AuditEvent.action == "sync.trigger",
                    AuditEvent.resource_id == str(connection_id),
                    AuditEvent.payload["origin"].astext == "schedule",
                    AuditEvent.timestamp >= now.replace(hour=0, minute=0, second=0, microsecond=0),
                )
                .order_by(AuditEvent.timestamp.desc())
                .limit(3)
            )
        ).all()
        if len(attempts) >= 3 or (attempts and now - attempts[0] < timedelta(minutes=15)):
            await db.commit()
            return {"status": "deferred"}
    task_id = str(uuid4())
    payload = {
        "provider": "solidus",
        "task_id": task_id,
        "requested_at": now.isoformat(),
        "origin": "schedule" if daily else "manual",
    }
    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="sync",
        action="sync.trigger",
        actor_id=actor_id,
        actor_type="user" if actor_id else "system",
        resource_type="connection",
        resource_id=str(connection_id),
        payload=payload,
    )
    await db.commit()
    try:
        await asyncio.wait_for(asyncio.to_thread(publish_refresh, tenant_id, connection_id, task_id), timeout=4)
    except Exception:
        await set_tenant_context(db, tenant_id)
        await audit_service.log_event(
            db=db,
            tenant_id=tenant_id,
            category="sync",
            action="sync.trigger_failed",
            actor_id=actor_id,
            actor_type="user" if actor_id else "system",
            resource_type="connection",
            resource_id=str(connection_id),
            payload=payload,
            status="error",
        )
        await db.commit()
        return {"status": "failed", "error_code": "sync_queue_unavailable"}
    return {"job_id": task_id, "status": "queued", "already_running": False}


async def refresh_due_sources(db, tenant_id, now):
    await set_tenant_context(db, tenant_id)
    predicates = (
        TransactionConfig.tenant_id == tenant_id,
        TransactionConfig.enabled.is_(True),
        TransactionConfig.schedule_enabled.is_(True),
        TransactionConfig.source_connection_id.is_not(None),
    )
    count = await db.scalar(
        select(func.count(func.distinct(TransactionConfig.source_connection_id))).where(*predicates)
    )
    offset = (int(now.timestamp()) // 60 * 25) % count if count else 0
    identifiers = (
        await db.scalars(
            select(TransactionConfig.source_connection_id)
            .where(*predicates)
            .distinct()
            .order_by(TransactionConfig.source_connection_id)
            .offset(offset)
            .limit(25)
        )
    ).all()
    await db.commit()
    queued = 0
    for identifier in identifiers:
        result = await queue_refresh(db, tenant_id, identifier, daily=True, now=now)
        queued += result["status"] == "queued" and not result.get("already_running")
    return queued
