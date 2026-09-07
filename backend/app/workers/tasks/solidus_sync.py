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
    **kwargs,
):
    tenant_id, connection_id = str(uuid.UUID(tenant_id)), str(uuid.UUID(connection_id))
    request_id = str(uuid.UUID(refresh_request_id)) if refresh_request_id else str(uuid.uuid4())
    if type(pages_remaining) is not int or not 1 <= pages_remaining <= MAX_REFRESH_PAGES:
        raise SolidusImportError("invalid_import_budget")

    async def run():
        async with worker_async_session() as db:
            return await sync_solidus_orders(db, tenant_id, connection_id, max_pages=min(MAX_PAGES, pages_remaining))

    summary = asyncio.run(run())
    remaining = pages_remaining - max(1, summary["pages_read"])
    if summary["termination_reason"] == "budget":
        if remaining > 0 and summary["pages_read"] > 0:
            continuation = celery_app.send_task(
                "tasks.solidus_sync",
                queue="sync",
                kwargs={
                    "tenant_id": tenant_id,
                    "connection_id": connection_id,
                    "pages_remaining": remaining,
                    "refresh_request_id": request_id,
                    "correlation_id": request_id,
                },
            )
            summary["continuation_task_id"] = continuation.id
        else:
            summary["reason"] = "refresh_budget_exhausted" if remaining <= 0 else "deadline"
    return summary
