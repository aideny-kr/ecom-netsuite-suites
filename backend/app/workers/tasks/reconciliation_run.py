"""Celery task for async reconciliation execution."""

from __future__ import annotations

from datetime import date

import structlog

from app.workers.celery_app import celery_app

logger = structlog.get_logger()


@celery_app.task(name="tasks.reconciliation_run", bind=True, max_retries=1)
def reconciliation_run_task(
    self,
    tenant_id: str,
    date_from: str,
    date_to: str,
    subsidiary_id: str | None = None,
    payout_ids: list[str] | None = None,
    job_id: str | None = None,
) -> dict:
    """Run reconciliation as a Celery task.

    Uses sync wrapper around async ReconJobRunner.
    """
    import asyncio

    from app.core.database import async_session_factory
    from app.services.reconciliation.recon_job import ReconJobRunner

    async def _run() -> dict:
        async with async_session_factory() as db:
            runner = ReconJobRunner(db=db, tenant_id=tenant_id)
            summary = await runner.run(
                date_from=date.fromisoformat(date_from),
                date_to=date.fromisoformat(date_to),
                subsidiary_id=subsidiary_id,
                payout_ids=payout_ids,
                job_id=job_id,
            )
            return summary.model_dump(mode="json")

    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(_run())
        loop.close()
        return result
    except Exception as exc:
        logger.error("reconciliation_run_task.failed", error=str(exc))
        raise self.retry(exc=exc, countdown=30)
