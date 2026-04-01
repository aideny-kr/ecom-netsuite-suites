"""Celery task: sync NetSuite deposits via SuiteQL.

This is an async service that needs to run in an event loop,
so we use asyncio.run() inside the Celery task.
"""

import asyncio
import logging
from datetime import date, timedelta

from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(base=InstrumentedTask, name="tasks.netsuite_deposit_sync", queue="sync")
def netsuite_deposit_sync(
    tenant_id: str,
    date_from: str | None = None,
    date_to: str | None = None,
    **kwargs,
):
    """Sync NetSuite deposits for a tenant.

    Uses async internals (netsuite_client, get_valid_token) so wraps in asyncio.run().
    Default: last 90 days if no dates provided.
    """
    from app.core.database import async_session_factory

    async def _run():
        from app.services.ingestion.netsuite_deposit_sync import sync_netsuite_deposits

        today = date.today()
        d_from = date.fromisoformat(date_from) if date_from else today - timedelta(days=90)
        d_to = date.fromisoformat(date_to) if date_to else today

        async with async_session_factory() as db:
            result = await sync_netsuite_deposits(
                db=db,
                tenant_id=tenant_id,
                date_from=d_from,
                date_to=d_to,
            )
            return {
                "records_synced": result.records_synced,
                "records_new": result.records_new,
                "records_updated": result.records_updated,
                "errors": result.errors,
            }

    return asyncio.run(_run())
