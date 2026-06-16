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
    if payout_ids and match_level != "payout":
        # The order engine has no payout filter — silently dropping payout_ids
        # would reconcile the whole window instead of the requested payouts.
        raise ValueError("payout_ids requires match_level='payout'")
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


@celery_app.task(base=InstrumentedTask, name="tasks.reconciliation_run", bind=True)
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

    from app.core.database import set_tenant_context_session, worker_async_session

    # Attribute the run to the Job row InstrumentedTask created for this
    # execution, so reconciliation_runs.job_id links back to the jobs table.
    effective_job_id = job_id or (str(self._job_id) if self._job_id else None)

    async def _run() -> dict:
        async with worker_async_session() as db:
            # Session-scoped SET (not SET LOCAL): both engines commit mid-run,
            # which would clear a transaction-scoped GUC for everything after
            # the first commit. Safe here because the engine is disposable.
            await set_tenant_context_session(db, tenant_id)
            return await _execute(
                db,
                tenant_id=tenant_id,
                date_from=date_from,
                date_to=date_to,
                subsidiary_id=subsidiary_id,
                payout_ids=payout_ids,
                job_id=effective_job_id,
                match_level=match_level,
            )

    # No in-task retry: the runner commits a failed-run row before raising, so a
    # retry would create a second ReconciliationRun. InstrumentedTask records the
    # failure; the nightly Beat schedule is the retry.
    return asyncio.run(_run())
