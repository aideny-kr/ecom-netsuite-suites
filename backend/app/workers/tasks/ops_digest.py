"""Daily ops digest worker: one instrumented job row, one audit row per tenant."""

import asyncio
from datetime import datetime, timezone

from app.core.database import worker_async_session
from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app


@celery_app.task(
    base=InstrumentedTask,
    name="tasks.ops_digest",
    queue="default",
    max_retries=0,
    soft_time_limit=540,
    time_limit=600,
)
def ops_digest_task():
    """Collect and deliver the digest for every active tenant; read-only."""

    async def execute():
        from app.services.ops_digest import run_ops_digest

        async with worker_async_session() as db:
            return await run_ops_digest(db, now=datetime.now(timezone.utc))

    return asyncio.run(execute())
