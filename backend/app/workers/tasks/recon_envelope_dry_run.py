"""Rung-1 dry-run job (Bet 3): report-only autonomy-envelope evaluation.

For each tenant with ``autonomous_recon`` enabled, evaluates the v1 envelope
against the latest COMPLETED reconciliation run and writes ONE audit event
(actor_type="system") with the report. NEVER mutates result rows; NEVER
writes to NetSuite. This builds the evidence base for enforcement later.
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

AUTONOMY_FLAG = "autonomous_recon"
MASTER_RECON_FLAG = "reconciliation"
DRY_RUN_ACTION = "recon.envelope.dry_run"


async def dry_run_for_tenant(db: AsyncSession, tenant_id: str) -> dict:
    """Evaluate the envelope for one tenant. Report-only: one audit event, no mutations."""
    from app.models.audit import AuditEvent
    from app.models.reconciliation import ReconciliationResult, ReconciliationRun
    from app.services import audit_service, feature_flag_service
    from app.services.reconciliation import autonomy_envelope

    tid = uuid.UUID(tenant_id)
    if not await feature_flag_service.is_enabled(db, tid, MASTER_RECON_FLAG):
        return {"tenant_id": tenant_id, "skipped": "reconciliation_disabled"}
    if not await feature_flag_service.is_enabled(db, tid, AUTONOMY_FLAG):
        return {"tenant_id": tenant_id, "skipped": "flag_disabled"}

    run = (
        await db.execute(
            select(ReconciliationRun)
            .where(
                ReconciliationRun.tenant_id == tid,
                ReconciliationRun.status == "completed",
            )
            .order_by(ReconciliationRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if run is None:
        return {"tenant_id": tenant_id, "skipped": "no_completed_run"}

    # One dry-run audit per run, ever — a tenant with no new runs must not be
    # re-audited nightly forever.
    already = (
        await db.execute(
            select(AuditEvent.id)
            .where(
                AuditEvent.tenant_id == tid,
                AuditEvent.action == DRY_RUN_ACTION,
                AuditEvent.resource_id == str(run.id),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if already is not None:
        return {"tenant_id": tenant_id, "run_id": str(run.id), "skipped": "already_evaluated"}

    results = (
        (
            await db.execute(
                select(ReconciliationResult).where(
                    ReconciliationResult.tenant_id == tid,
                    ReconciliationResult.run_id == run.id,
                )
            )
        )
        .scalars()
        .all()
    )
    report = autonomy_envelope.evaluate(results)

    await audit_service.log_event(
        db=db,
        tenant_id=tid,
        category="reconciliation",
        action=DRY_RUN_ACTION,
        actor_id=None,
        actor_type="system",
        resource_type="reconciliation_run",
        resource_id=str(run.id),
        correlation_id=f"envelope-dryrun-{uuid.uuid4().hex}",
        payload=report.to_payload(),
    )
    await db.commit()
    return {"tenant_id": tenant_id, "run_id": str(run.id), **report.to_payload()}


@celery_app.task(base=InstrumentedTask, name="tasks.recon_envelope_dry_run", queue="recon")
def recon_envelope_dry_run(tenant_id: str, **kwargs):
    """Per-tenant dry run. Opens its own RLS-scoped session."""
    import asyncio

    from app.core.database import set_tenant_context, worker_async_session

    async def _run() -> dict:
        async with worker_async_session() as db:
            await set_tenant_context(db, tenant_id)
            return await dry_run_for_tenant(db, tenant_id)

    return asyncio.run(_run())


@celery_app.task(base=InstrumentedTask, name="tasks.recon_envelope_dry_run_all", queue="recon")
def recon_envelope_dry_run_all():
    """Beat fan-out: one dry-run task per autonomous_recon-enabled tenant."""
    import asyncio

    from app.core.database import worker_async_session
    from app.services import feature_flag_service

    async def _tenants() -> list[str]:
        async with worker_async_session() as db:
            autonomy = set(await feature_flag_service.list_enabled_tenants(db, AUTONOMY_FLAG))
            master = set(await feature_flag_service.list_enabled_tenants(db, MASTER_RECON_FLAG))
            return [str(t) for t in sorted(autonomy & master, key=str)]

    stats = {"dispatched": 0, "failed": 0}
    for tenant_id in asyncio.run(_tenants()):
        try:
            celery_app.send_task(
                "tasks.recon_envelope_dry_run",
                kwargs={"tenant_id": tenant_id},
                queue="recon",
            )
            stats["dispatched"] += 1
        except Exception:
            stats["failed"] += 1
            logger.exception("recon_envelope_dry_run_all.dispatch_failed", extra={"tenant_id": tenant_id})
    logger.info("recon_envelope_dry_run_all.completed", extra=stats)
    return stats
