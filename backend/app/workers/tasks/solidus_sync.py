"""Resume a read-only order mirror within a finite refresh budget."""

import asyncio
import uuid

from app.core.database import worker_async_session
from app.services.ingestion.solidus_sync import MAX_PAGES, SolidusImportError, sync_solidus_orders
from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

MAX_REFRESH_PAGES = 500


@celery_app.task(base=InstrumentedTask, name="tasks.solidus_sync", queue="sync", soft_time_limit=210, time_limit=240)
def solidus_sync(
    tenant_id: str,
    connection_id: str,
    pages_remaining: int = MAX_REFRESH_PAGES,
    refresh_request_id: str | None = None,
    schedule_config_id: str | None = None,
    **kwargs,
):
    tenant_id, connection_id = str(uuid.UUID(tenant_id)), str(uuid.UUID(connection_id))
    request_id = str(uuid.UUID(refresh_request_id)) if refresh_request_id else str(uuid.uuid4())
    if type(pages_remaining) is not int or not 1 <= pages_remaining <= MAX_REFRESH_PAGES:
        raise SolidusImportError("invalid_import_budget")

    async def run():
        async with worker_async_session() as db:
            if refresh_request_id and schedule_config_id is None:
                from sqlalchemy import select

                from app.core.database import set_tenant_context
                from app.models.audit import AuditEvent

                await set_tenant_context(db, tenant_id)
                event = await db.scalar(
                    select(AuditEvent)
                    .where(
                        AuditEvent.tenant_id == uuid.UUID(tenant_id),
                        AuditEvent.action == "sync.trigger",
                        AuditEvent.resource_id == connection_id,
                        AuditEvent.payload["task_id"].astext == request_id,
                    )
                    .limit(1)
                )
                if event and event.payload.get("origin") == "schedule":
                    raise SolidusImportError("legacy_scheduled_refresh_requires_requeue")
            return await sync_solidus_orders(
                db,
                tenant_id,
                connection_id,
                max_pages=min(MAX_PAGES, pages_remaining),
                schedule_config_id=schedule_config_id,
            )

    summary = asyncio.run(run())
    remaining = pages_remaining - max(1, summary["pages_read"])
    contention = summary["termination_reason"] == "stall" and summary.get("reason") == "refresh_in_progress"
    if summary["termination_reason"] == "budget" or contention:
        if remaining > 0 and summary["pages_read"] > 0:
            continuation = celery_app.send_task(
                "tasks.solidus_sync",
                queue="sync",
                countdown=5 if contention else 0,
                kwargs={
                    "tenant_id": tenant_id,
                    "connection_id": connection_id,
                    "pages_remaining": remaining,
                    "refresh_request_id": request_id,
                    "schedule_config_id": schedule_config_id,
                    "correlation_id": request_id,
                },
            )
            summary["continuation_task_id"] = continuation.id
        else:
            summary["reason"] = "refresh_budget_exhausted" if remaining <= 0 else "deadline"
    return summary
