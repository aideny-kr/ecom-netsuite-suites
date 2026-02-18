"""Celery task for executing workspace runs (validate, tests, assertions, deploy)."""

import asyncio
import uuid

from app.core.database import async_session_factory, set_tenant_context
from app.services import runner_service
from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app


@celery_app.task(base=InstrumentedTask, bind=True, name="tasks.workspace_run", queue="default")
def workspace_run_task(self, tenant_id: str, run_id: str, **kwargs):
    """Execute a workspace run inside an async context."""
    extra_params = kwargs.get("extra_params")
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_execute(tenant_id, run_id, extra_params=extra_params))
    finally:
        loop.close()


async def _execute(tenant_id: str, run_id: str, extra_params: dict | None = None) -> dict:
    """Async inner: open session, set RLS, execute run."""
    async with async_session_factory() as session:
        await set_tenant_context(session, tenant_id)
        run = await runner_service.execute_run(
            session, uuid.UUID(run_id), uuid.UUID(tenant_id), extra_params=extra_params
        )
        await session.commit()
        return {
            "run_id": str(run.id),
            "status": run.status,
            "exit_code": run.exit_code,
            "duration_ms": run.duration_ms,
        }
