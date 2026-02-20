"""Celery task for async NetSuite metadata discovery.

Runs on the 'sync' queue. Discovers custom fields, record types, and
organisational hierarchies from the tenant's NetSuite account.
"""

import asyncio
import uuid

from app.core.database import set_tenant_context, worker_async_session
from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app


@celery_app.task(
    base=InstrumentedTask,
    bind=True,
    name="tasks.netsuite_metadata_discovery",
    queue="sync",
    soft_time_limit=120,
    time_limit=180,
)
def netsuite_metadata_discovery(self, tenant_id: str, user_id: str | None = None, **kwargs):
    """Discover NetSuite metadata (custom fields, org hierarchy) for a tenant."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_execute(tenant_id, user_id))
    finally:
        loop.close()


async def _execute(tenant_id: str, user_id: str | None) -> dict:
    """Async inner: open session, set RLS, run discovery."""
    from app.services.netsuite_metadata_service import run_full_discovery

    async with worker_async_session() as session:
        await set_tenant_context(session, tenant_id)
        metadata = await run_full_discovery(
            db=session,
            tenant_id=uuid.UUID(tenant_id),
            user_id=uuid.UUID(user_id) if user_id else None,
        )
        return {
            "status": metadata.status,
            "version": metadata.version,
            "queries_succeeded": metadata.query_count,
            "total_fields_discovered": metadata.total_fields_discovered,
        }
