"""Rung-1 dry-run job (Bet 3): report-only autonomy-envelope evaluation.

For each tenant with ``autonomous_recon`` (and the master ``reconciliation``
flag) enabled, evaluates the v1 envelope against every not-yet-evaluated
COMPLETED reconciliation run in the recent window — newest first, bounded —
and writes ONE audit event (actor_type="system") per run with the report.
Catch-up by design: coverage of the evidence base must not depend on Beat
timing vs run duration (a run completing after the 04:30 slot, or superseded
by a manual run, is picked up on the next pass). NEVER mutates result rows;
NEVER writes to NetSuite.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

AUTONOMY_FLAG = "autonomous_recon"
MASTER_RECON_FLAG = "reconciliation"
DRY_RUN_ACTION = "recon.envelope.dry_run"
# Catch-up bounds. The window also keeps the audit-event dedup horizon safely
# inside AUDIT_RETENTION_DAYS (90) — retention can never resurrect an old run.
MAX_RUNS_PER_EVALUATION = 10
EVALUATION_WINDOW_DAYS = 30


async def dry_run_for_tenant(db: AsyncSession, tenant_id: str) -> dict:
    """Evaluate the envelope for one tenant. Report-only: audit events only, no mutations."""
    from app.models.audit import AuditEvent
    from app.models.reconciliation import ReconciliationResult, ReconciliationRun
    from app.services import audit_service, feature_flag_service
    from app.services.reconciliation import autonomy_envelope

    tid = uuid.UUID(tenant_id)
    if not await feature_flag_service.is_enabled(db, tid, MASTER_RECON_FLAG):
        return {"tenant_id": tenant_id, "skipped": "reconciliation_disabled"}
    if not await feature_flag_service.is_enabled(db, tid, AUTONOMY_FLAG):
        return {"tenant_id": tenant_id, "skipped": "flag_disabled"}

    cutoff = datetime.now(timezone.utc) - timedelta(days=EVALUATION_WINDOW_DAYS)
    run_window = (
        ReconciliationRun.tenant_id == tid,
        ReconciliationRun.status == "completed",
        ReconciliationRun.created_at >= cutoff,
    )
    # One dry-run audit per run, ever. The audited filter applies BEFORE the
    # per-pass cap — otherwise a busy tenant's newest N audited runs occupy the
    # window forever and older unevaluated runs are never reached (codex P2).
    # resource_id is NOT NULL for this action, but guard the NOT-IN-NULL trap.
    audited_ids = select(AuditEvent.resource_id).where(
        AuditEvent.tenant_id == tid,
        AuditEvent.action == DRY_RUN_ACTION,
        AuditEvent.resource_id.is_not(None),
    )
    runs = (
        (
            await db.execute(
                select(ReconciliationRun)
                .where(*run_window, cast(ReconciliationRun.id, String).notin_(audited_ids))
                .order_by(ReconciliationRun.created_at.desc())
                .limit(MAX_RUNS_PER_EVALUATION)
            )
        )
        .scalars()
        .all()
    )
    already_evaluated_count = (
        await db.execute(
            select(func.count())
            .select_from(ReconciliationRun)
            .where(*run_window, cast(ReconciliationRun.id, String).in_(audited_ids))
        )
    ).scalar_one()
    if not runs and already_evaluated_count == 0:
        return {"tenant_id": tenant_id, "skipped": "no_completed_run"}

    reports: list[dict] = []
    for run in runs:
        # Column-only select: the evaluator reads six scalars; loading full ORM
        # entities (incl. evidence JSON) for tens of thousands of rows is waste.
        rows = (
            await db.execute(
                select(
                    ReconciliationResult.id,
                    ReconciliationResult.status,
                    ReconciliationResult.bucket,
                    ReconciliationResult.match_type,
                    ReconciliationResult.variance_amount,
                    ReconciliationResult.stripe_amount,
                ).where(
                    ReconciliationResult.tenant_id == tid,
                    ReconciliationResult.run_id == run.id,
                )
            )
        ).all()
        payload = autonomy_envelope.evaluate(rows).to_payload()

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
            payload=payload,
        )
        reports.append({"run_id": str(run.id), **payload})

    await db.commit()
    return {
        "tenant_id": tenant_id,
        "evaluated_count": len(reports),
        "already_evaluated_count": already_evaluated_count,
        "reports": reports,
    }


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
            tenants = await feature_flag_service.list_tenants_with_flags(db, (AUTONOMY_FLAG, MASTER_RECON_FLAG))
            return [str(t) for t in tenants]

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
