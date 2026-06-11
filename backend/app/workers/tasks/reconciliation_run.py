"""Celery task for async reconciliation execution."""

from __future__ import annotations

from datetime import date

import structlog

from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

logger = structlog.get_logger()


async def _execute(
    db,
    tenant_id: str,
    date_from: str,
    date_to: str,
    subsidiary_id: str | None,
    payout_ids: list[str] | None,
    job_id: str | None,
    match_level: str,
) -> dict:
    """Route to the right engine — mirrors POST /reconciliation/runs (create_run).

    match_level="order" (product default) → OrderReconJob, which carries all the
    R1/R2 hardening; match_level="payout" → legacy ReconJobRunner (payout_ids).
    """
    if match_level == "order":
        from app.services.reconciliation.order_recon_job import OrderReconJob

        order_job = OrderReconJob(db=db, tenant_id=tenant_id)
        summary = await order_job.run(
            date_from=date.fromisoformat(date_from),
            date_to=date.fromisoformat(date_to),
            subsidiary_id=subsidiary_id,
            job_id=job_id,
        )
    else:
        from app.services.reconciliation.recon_job import ReconJobRunner

        runner = ReconJobRunner(db=db, tenant_id=tenant_id)
        summary = await runner.run(
            date_from=date.fromisoformat(date_from),
            date_to=date.fromisoformat(date_to),
            subsidiary_id=subsidiary_id,
            payout_ids=payout_ids,
            job_id=job_id,
        )
    return summary.model_dump(mode="json")


@celery_app.task(base=InstrumentedTask, name="tasks.reconciliation_run", bind=True, max_retries=1)
def reconciliation_run_task(
    self,
    tenant_id: str,
    date_from: str,
    date_to: str,
    subsidiary_id: str | None = None,
    payout_ids: list[str] | None = None,
    job_id: str | None = None,
    match_level: str = "order",
) -> dict:
    """Run reconciliation as a Celery task.

    Routes exactly like the user-facing create_run endpoint: match_level="order"
    (default) runs the order-level OrderReconJob; match_level="payout" runs the
    legacy payout-level ReconJobRunner.
    """
    import asyncio

    from app.core.database import async_session_factory

    async def _run() -> dict:
        async with async_session_factory() as db:
            return await _execute(
                db,
                tenant_id=tenant_id,
                date_from=date_from,
                date_to=date_to,
                subsidiary_id=subsidiary_id,
                payout_ids=payout_ids,
                job_id=job_id,
                match_level=match_level,
            )

    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(_run())
        loop.close()
        return result
    except Exception as exc:
        logger.error("reconciliation_run_task.failed", error=str(exc))
        raise self.retry(exc=exc, countdown=30)
