"""Durable, budgeted investigation adapters. Repairs require a separate human decision."""

import asyncio
import uuid
from datetime import datetime, timezone

from app.core.database import set_tenant_context, worker_async_session
from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app


@celery_app.task(
    base=InstrumentedTask,
    name="tasks.transaction_ops_run",
    queue="recon",
    max_retries=0,
    soft_time_limit=3650,
    time_limit=3700,
)
def transaction_ops_run(tenant_id: str, run_id: str):
    async def execute():
        from app.services.transaction_ops.runner import run_investigation

        tenant, run = uuid.UUID(tenant_id), uuid.UUID(run_id)
        async with worker_async_session() as db:
            await set_tenant_context(db, str(tenant))
            return await run_investigation(db, tenant, run)

    try:
        return asyncio.run(execute())
    except Exception:
        # InstrumentedTask persists str(exc); keep upstream details out of it.
        raise RuntimeError("transaction_investigation_failed") from None


@celery_app.task(
    base=InstrumentedTask,
    name="tasks.transaction_ops_collect_due",
    queue="recon",
    max_retries=0,
    soft_time_limit=50,
    time_limit=55,
)
def transaction_ops_collect_due():
    async def execute():
        from app.services.transaction_ops.scheduler import collect_due_runs

        async with worker_async_session() as db:
            return await collect_due_runs(db, datetime.now(timezone.utc))

    try:
        return asyncio.run(execute())
    except Exception:
        raise RuntimeError("transaction_scheduler_failed") from None
