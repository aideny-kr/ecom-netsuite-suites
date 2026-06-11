"""Scheduled Celery task: nightly reconciliation runs for opted-in tenants.

Dispatches the existing ``tasks.reconciliation_run`` per tenant whose
``recon_scheduled_runs`` feature flag is enabled, always with
``match_level="order"`` — the order-level OrderReconJob engine (the product
default, same as a user-triggered run). Read+match only — results land in
reconciliation_runs/_results; no NetSuite writes, no approvals (Bet 3 Rung 1
groundwork).
"""

import logging
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

SCHEDULED_RUN_FLAG = "recon_scheduled_runs"
SCHEDULED_RUN_WINDOW_DAYS = 7


async def collect_and_dispatch(db: AsyncSession) -> dict:
    """Find flag-enabled tenants and enqueue one reconciliation run each."""
    from app.services import feature_flag_service

    tenant_ids = await feature_flag_service.list_enabled_tenants(db, SCHEDULED_RUN_FLAG)

    today = date.today()
    date_from = (today - timedelta(days=SCHEDULED_RUN_WINDOW_DAYS)).isoformat()
    date_to = today.isoformat()

    stats = {"dispatched": 0, "failed": 0}
    for tenant_id in tenant_ids:
        try:
            celery_app.send_task(
                "tasks.reconciliation_run",
                kwargs={
                    "tenant_id": str(tenant_id),
                    "date_from": date_from,
                    "date_to": date_to,
                    "match_level": "order",
                },
                queue="recon",
            )
            stats["dispatched"] += 1
        except Exception:
            stats["failed"] += 1
            logger.exception("recon_scheduled_run_all.dispatch_failed", extra={"tenant_id": str(tenant_id)})
    logger.info("recon_scheduled_run_all.completed", extra=stats)
    return stats


@celery_app.task(base=InstrumentedTask, name="tasks.recon_scheduled_run_all", queue="recon")
def recon_scheduled_run_all():
    """Beat entry point. Opens its own session; logic lives in collect_and_dispatch()."""
    import asyncio

    from app.core.database import async_session_factory

    async def _run() -> dict:
        async with async_session_factory() as db:
            return await collect_and_dispatch(db)

    return asyncio.run(_run())
