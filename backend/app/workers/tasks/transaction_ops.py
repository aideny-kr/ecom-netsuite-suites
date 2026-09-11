"""Durable, budgeted investigation adapters. Repairs require a separate human decision."""

import asyncio
import uuid
from datetime import datetime, timezone

from app.core.database import set_tenant_context, worker_async_session
from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import RECON_COLLECTOR_PRIORITY, RECON_COLLECTOR_QUEUE, celery_app


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
            result = await run_investigation(db, tenant, run)
            child = None
            if result.get("termination_reason") == "budget":
                from app.services.transaction_ops.continuation import continue_budget_run

                child = await continue_budget_run(db, tenant, run)
            elif result.get("termination_reason") == "done":
                from app.services.transaction_ops.period_review import continue_review

                child = await continue_review(db, tenant, run)
            if child is not None:
                from app.services.transaction_ops.scheduler import _dispatch

                result["continuation_run_id"] = str(child.id)
                stats = {"dispatched": 0, "dispatch_failed": 0}
                if child.status == "pending":
                    await _dispatch(tenant, child.id, stats)
                result.update(stats)
            return result

    try:
        return asyncio.run(execute())
    except Exception:
        # InstrumentedTask persists str(exc); keep upstream details out of it.
        raise RuntimeError("transaction_investigation_failed") from None


@celery_app.task(
    base=InstrumentedTask,
    name="tasks.transaction_ops_collect_due",
    queue=RECON_COLLECTOR_QUEUE,
    priority=RECON_COLLECTOR_PRIORITY,
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


@celery_app.task(
    base=InstrumentedTask,
    name="tasks.transaction_ops_execute",
    queue="recon",
    max_retries=0,
    soft_time_limit=330,
    time_limit=340,
)
def transaction_ops_execute(tenant_id: str, proposal_id: str):
    async def execute():
        from app.services.transaction_ops.executor import execute_proposal

        tenant, proposal = uuid.UUID(tenant_id), uuid.UUID(proposal_id)
        async with worker_async_session() as db:
            await set_tenant_context(db, str(tenant))
            return await execute_proposal(db, tenant, proposal)

    try:
        return asyncio.run(execute())
    except Exception:
        raise RuntimeError("transaction_execution_failed") from None


@celery_app.task(
    base=InstrumentedTask,
    name="tasks.transaction_ops_recover",
    queue="recon",
    max_retries=0,
    soft_time_limit=330,
    time_limit=340,
)
def transaction_ops_recover(tenant_id: str, operation_id: str):
    async def execute():
        from app.services.transaction_ops.recovery import recover_operation

        tenant, operation = uuid.UUID(tenant_id), uuid.UUID(operation_id)
        async with worker_async_session() as db:
            await set_tenant_context(db, str(tenant))
            return await recover_operation(db, tenant, operation)

    try:
        return asyncio.run(execute())
    except Exception:
        raise RuntimeError("transaction_recovery_failed") from None


@celery_app.task(
    base=InstrumentedTask,
    name="tasks.transaction_ops_collect_actions",
    queue=RECON_COLLECTOR_QUEUE,
    priority=RECON_COLLECTOR_PRIORITY,
    max_retries=0,
    soft_time_limit=50,
    time_limit=55,
)
def transaction_ops_collect_actions():
    async def execute():
        from app.services.transaction_ops.action_scheduler import collect_due_actions

        async with worker_async_session() as db:
            return await collect_due_actions(db, datetime.now(timezone.utc))

    try:
        return asyncio.run(execute())
    except Exception:
        raise RuntimeError("transaction_action_scheduler_failed") from None


@celery_app.task(
    base=InstrumentedTask,
    name="tasks.transaction_ops_recover_credit",
    queue="recon",
    max_retries=0,
    soft_time_limit=110,
    time_limit=120,
)
def transaction_ops_recover_credit(tenant_id: str, message_id: str):
    async def execute():
        from app.services.transaction_ops.accounting_recovery import recover

        async with worker_async_session() as db:
            return await recover(db, uuid.UUID(tenant_id), uuid.UUID(message_id), lock_engine=db.bind)

    try:
        return asyncio.run(execute())
    except Exception:
        raise RuntimeError("accounting_credit_recovery_failed") from None
