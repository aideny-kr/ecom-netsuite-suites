"""Tenant-scoped refresh status from existing jobs, audit requests and cursors."""

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models.audit import AuditEvent
from app.models.canonical import Order
from app.models.connection import Connection
from app.models.job import Job
from app.models.pipeline import CursorState
from app.services.ingestion.solidus_sync import CURSOR_TYPE, INITIAL_LOOKBACK_DAYS


async def solidus_sync_status(db, tenant_id, connection_id):
    raw = await db.scalar(
        select(CursorState.cursor_value)
        .join(Connection, Connection.id == CursorState.connection_id)
        .where(
            CursorState.connection_id == connection_id,
            CursorState.object_type == CURSOR_TYPE,
            Connection.tenant_id == tenant_id,
        )
    )
    try:
        cursor = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        cursor = {}
    job = await db.scalar(
        select(Job)
        .where(
            Job.tenant_id == tenant_id,
            Job.connection_id == connection_id,
            Job.job_type == "tasks.solidus_sync",
        )
        .order_by(Job.started_at.desc().nullslast(), Job.id.desc())
        .limit(1)
    )
    request = await db.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.category == "sync",
            AuditEvent.resource_type == "connection",
            AuditEvent.resource_id == str(connection_id),
            AuditEvent.action.in_(("sync.trigger", "sync.trigger_failed")),
        )
        .order_by(
            AuditEvent.payload["requested_at"].astext.desc().nullslast(),
            AuditEvent.timestamp.desc(),
            AuditEvent.id.desc(),
        )
        .limit(1)
    )
    count = await db.scalar(
        select(func.count())
        .select_from(Order)
        .where(
            Order.tenant_id == tenant_id,
            Order.source_connection_id == connection_id,
        )
    )
    result = {
        "connection_id": str(connection_id),
        "status": "never_synced",
        "job_id": None,
        "last_completed_at": cursor.get("completed_at"),
        "coverage_since": cursor.get("since"),
        "initial_lookback_days": INITIAL_LOOKBACK_DAYS,
        "records_imported": count or 0,
        "source_total": cursor.get("total"),
        "next_page": cursor.get("next_page"),
        "error_code": None,
    }
    if cursor:
        result["status"] = "partial" if cursor.get("next_page") else "completed"
    now = datetime.now(timezone.utc)
    if job and job.status == "running":
        result.update(status="running", job_id=job.celery_task_id)
        if job.started_at and now - job.started_at > timedelta(minutes=5):
            result.update(status="failed", error_code="refresh_interrupted")
        return result
    requested_at = None
    if request:
        try:
            requested_at = datetime.fromisoformat((request.payload or {}).get("requested_at", ""))
        except ValueError:
            requested_at = request.timestamp
    if requested_at and (not job or not job.started_at or requested_at > job.started_at):
        result.update(job_id=(request.payload or {}).get("task_id"))
        if request.action == "sync.trigger_failed":
            result.update(status="failed", error_code="sync_queue_unavailable")
        else:
            result["status"] = "queued" if now - requested_at < timedelta(minutes=5) else "delayed"
    elif job:
        result["job_id"] = job.celery_task_id
        summary = job.result_summary or {}
        if job.status == "failed":
            result.update(status="failed", error_code="source_refresh_failed")
        elif summary.get("continuation_task_id") and job.completed_at:
            result["status"] = "queued" if now - job.completed_at < timedelta(minutes=5) else "delayed"
        elif summary.get("complete") is False:
            result.update(status="partial", error_code=summary.get("reason"))
    return result
