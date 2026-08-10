"""Scheduled: recompute the envelope self-contradiction back-test and store it.

The back-test is the only evidence about the autonomy envelope that costs nothing —
no reviewer, 100% coverage. But it is an aggregate over the whole results table:
measured against live data on 2026-08-10, **4,776 ms**, sequential scan over 308,863
rows, external merge sort spilling 31 MB to disk.

So it must not be computed on a request. `/data-status` is loaded by the recon UI,
and trading a UI regression for a diagnostic is a bad deal. It is computed here, on a
schedule, and the endpoint reads the stored answer — a single indexed row.

Stored in ``jobs.result_summary`` rather than a new table. That buys three things for
free: history (so the number is trendable rather than a snapshot), visibility in the
ops digest, and — because ``InstrumentedTask`` writes a Job row on start — the ability
to detect that this task STOPPED RUNNING. A failure-only check reports all-clear when
work silently stops, and a stale bound presented as current is exactly the confident
wrong number this whole effort exists to avoid.

Weekly, not nightly: the input only changes when a window is re-reconciled, which is
an operational accident, and a 5-second full scan is not worth running daily to watch
a number that moves in months.
"""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

JOB_TYPE = "envelope_backtest"


async def compute_for_all_tenants(db: AsyncSession) -> dict[str, Any]:
    """Run the back-test per tenant that has reconciliation results at all.

    Tenants with no recon data are skipped rather than recorded as a clean zero: an
    empty population is not evidence of correctness, and a row saying
    ``contradictions: 0`` for a tenant that has never reconciled anything is exactly
    the kind of confident nothing this metric is supposed to refuse to emit.
    """
    from app.models.reconciliation import ReconciliationResult
    from app.services.reconciliation.envelope_backtest import envelope_self_contradiction

    tenant_ids = (
        (await db.execute(select(ReconciliationResult.tenant_id).group_by(ReconciliationResult.tenant_id)))
        .scalars()
        .all()
    )

    per_tenant: dict[str, Any] = {}
    for tid in tenant_ids:
        try:
            per_tenant[str(tid)] = await envelope_self_contradiction(db, tenant_id=tid)
        except Exception as exc:  # one tenant's failure must not lose the others
            logger.warning("envelope_backtest failed for tenant %s: %s", tid, exc)
            per_tenant[str(tid)] = {"error": str(exc)[:200]}

    contradicting = [t for t, v in per_tenant.items() if (v.get("contradictions") or 0) > 0]
    return {
        "tenants_measured": len(per_tenant),
        # Surfaced at the top level so the ops digest can alarm on it without
        # understanding the payload shape.
        "tenants_with_contradictions": len(contradicting),
        "per_tenant": per_tenant,
    }


@celery_app.task(base=InstrumentedTask, name="tasks.envelope_backtest", queue="recon")
def envelope_backtest(**kwargs) -> dict[str, Any]:
    """Entry point. Returns the payload InstrumentedTask stores in result_summary."""
    import asyncio

    from app.core.database import async_session_factory

    async def _run() -> dict[str, Any]:
        async with async_session_factory() as db:
            return await compute_for_all_tenants(db)

    return asyncio.run(_run())
