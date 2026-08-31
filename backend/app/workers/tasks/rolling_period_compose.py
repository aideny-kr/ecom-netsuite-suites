"""Rolling period Stage 2 — the scheduled compose that makes the dashboard wall roll.

Stage 1 gave a ``ReportSeries`` a wall that follows its NEWEST report, but nothing
created the next period's report: the wall only moved when a person composed one by
hand. This is that missing half.

Shape (`.claude/rules/agent-graph.md` #13): there is no quality floor to defend, no
latency ceiling, and no divergent input kinds — so this is **a straight pipeline**, not a
fan-out or an agent loop. #15 adds: Beat's cadence is already the scheduling layer, and
this pipeline is cheap to re-run, so no durable-execution framework is imported.

Idempotency (#10) is inherited, not invented. A tracking compose is keyed on
``(series_id, period)``, enforced by the partial unique index
``uq_reports_series_id_period`` and an ON CONFLICT upsert inside
``compose_playbook_report``. Re-running this sweep is therefore free and safe, and a
crash mid-sweep needs no compensation — which is also why #9 (irreversible last) costs
nothing here: composing writes only our own rows and performs NetSuite READS only.

Cost (#6) is bounded because it is real: one tracking compose is 4 NetSuite round trips
(balance_sheet, trial_balance) or 6 (income_statement — 2 for period resolution, 4 for
sources), against a 120/min per-tenant ceiling that is SHARED with live chat traffic. An
unbounded nightly sweep can therefore starve real users, so ``max_composes`` caps the
work per tenant per run and leftover work is reported as ``budget`` rather than hidden.

Termination (#5) returns a reason enum — ``done | budget | stall | error`` — never a
boolean, so a human (and the ops digest) can route "finished" apart from "stuck".
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.report import Report
from app.models.report_series import ReportSeries
from app.models.tenant import Tenant
from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

# stdlib logger, and every call site passes context via `extra=` — NOT structlog kwargs.
# Celery hijacks the root logger, so a structlog-style `logger.info("x", key=v)` call
# raises TypeError inside the worker while passing fine under pytest (see
# project_report_auto_refresh_first_organic_run_failed).
logger = logging.getLogger(__name__)

#: Terminal reasons. Mirrors agent-graph.md #5 exactly — do not add a fifth without
#: deciding how the ops digest should route it.
REASON_DONE = "done"
REASON_BUDGET = "budget"
REASON_STALL = "stall"
REASON_ERROR = "error"


def _fingerprint(exc: BaseException) -> str:
    """Collapse a failure to a comparable shape. Identical fingerprints across every
    attempt means re-running will not help — that is a stall, not a slow convergence,
    and it is separately routable from a one-off error."""
    return f"{type(exc).__name__}:{str(exc)[:120]}"


async def sweep_tenant_series(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    max_composes: int | None = None,
) -> dict:
    """Compose the current closed period for every series of one tenant that lacks it.

    Resolves the closed period ONCE per tenant (not once per series): the resolver is 2
    NetSuite round trips, and every series of a tenant shares the same answer.

    Returns a ``result_summary`` dict whose ``reason`` is the terminal enum.
    """
    # Imported at call time, not module scope: `from X import Y` inside a function body
    # reads the CURRENT X.Y, which is what a monkeypatch set up before the call reaches.
    from app.services.report.period_resolver import resolve_last_closed_period_cached

    if max_composes is None:
        max_composes = settings.ROLLING_PERIOD_COMPOSE_MAX_PER_TENANT

    stats: dict = {
        "tenant_id": str(tenant_id),
        "period": None,
        "series_total": 0,
        "composed": 0,
        "already_current": 0,
        "failed": 0,
        "remaining": 0,
        "detail": None,
    }

    closed = await resolve_last_closed_period_cached(db, tenant_id)
    if not closed.resolved or not closed.name:
        # The run cannot do its job. Report `error` and compose nothing — reporting
        # `done` here would be the "absence is not success" failure: a sweep that
        # changed nothing because it was blind looks identical to one that found
        # nothing to do.
        reason_detail = closed.reason.value if closed.reason else "unknown"
        stats["reason"] = REASON_ERROR
        stats["detail"] = f"could not resolve the last closed period: {reason_detail}"
        logger.warning("rolling_period_compose.period_unresolved", extra=dict(stats))
        return stats

    stats["period"] = closed.name

    series_rows = (
        await db.execute(select(ReportSeries.id, ReportSeries.playbook_key).where(ReportSeries.tenant_id == tenant_id))
    ).all()
    stats["series_total"] = len(series_rows)
    if not series_rows:
        stats["reason"] = REASON_DONE
        return stats

    # The cheap filter before the expensive call (#14): one indexed query tells us which
    # series already hold this period, instead of paying 4-6 NetSuite round trips per
    # series to rediscover it inside compose's own pre-flight.
    covered = set(
        (
            await db.execute(
                select(Report.series_id).where(
                    Report.tenant_id == tenant_id,
                    Report.period == closed.name,
                    Report.series_id.in_([row.id for row in series_rows]),
                )
            )
        )
        .scalars()
        .all()
    )
    behind = [row for row in series_rows if row.id not in covered]
    stats["already_current"] = len(series_rows) - len(behind)

    fingerprints: set[str] = set()
    for index, row in enumerate(behind):
        if stats["composed"] >= max_composes:
            # Budget ceiling reached with work still on the table. Surfacing this as
            # `budget` (not `done`) is the whole point of the enum: `done` would tell
            # the next reader there is nothing left to do.
            stats["remaining"] = len(behind) - index
            stats["reason"] = REASON_BUDGET
            logger.info("rolling_period_compose.budget_reached", extra=dict(stats))
            return stats

        from app.services.report.playbooks import compose_playbook_report

        try:
            await compose_playbook_report(
                db,
                playbook_key=row.playbook_key,
                params={},
                tenant_id=tenant_id,
                actor_id=None,
                mode="tracking",
                # No person is behind a scheduled compose. Attributing it to a user
                # would put a false record in the audit trail; refresh_report threads
                # actor_type="system" for exactly this reason.
                actor_type="system",
            )
            stats["composed"] += 1
        except Exception as exc:  # per-series isolation: one bad series must not
            # abort the rest of the tenant's sweep.
            stats["failed"] += 1
            fingerprints.add(_fingerprint(exc))
            logger.exception(
                "rolling_period_compose.series_failed",
                extra={"tenant_id": str(tenant_id), "playbook_key": row.playbook_key},
            )

    if stats["composed"] == 0 and stats["failed"] > 0:
        if len(fingerprints) == 1:
            stats["reason"] = REASON_STALL
            stats["detail"] = next(iter(fingerprints))
        else:
            stats["reason"] = REASON_ERROR
            stats["detail"] = f"{stats['failed']} series failed with {len(fingerprints)} distinct failures"
    else:
        stats["reason"] = REASON_DONE

    logger.info("rolling_period_compose.completed", extra=dict(stats))
    return stats


async def collect_and_dispatch(db: AsyncSession) -> dict:
    """Beat entry logic: fan one task out per active tenant.

    Gated on a single global setting, defaulting OFF — the same mechanism
    ``report_auto_refresh_all`` uses. Fail-closed: a deployment that has never heard of
    this feature does nothing rather than sweeping every tenant on first boot.
    """
    stats = {"tenants": 0, "dispatched": 0, "failed": 0, "enabled": settings.ROLLING_PERIOD_AUTO_COMPOSE_ENABLED}
    if not settings.ROLLING_PERIOD_AUTO_COMPOSE_ENABLED:
        stats["reason"] = REASON_DONE
        stats["detail"] = "ROLLING_PERIOD_AUTO_COMPOSE_ENABLED is false"
        logger.info("rolling_period_compose_all.disabled", extra=dict(stats))
        return stats

    tenant_ids = (await db.execute(select(Tenant.id).where(Tenant.is_active.is_(True)))).scalars().all()
    stats["tenants"] = len(tenant_ids)
    for tenant_id in tenant_ids:
        try:
            # kwargs, NOT positional: InstrumentedTask.before_start/on_success read
            # `kwargs.get("tenant_id")` to decide which tenant the Job row belongs to
            # (base_task.py:57,90), falling back to SYSTEM_TENANT_ID. A positional
            # dispatch files every per-tenant run under the system tenant, which makes
            # the ops digest unable to attribute a failure to the tenant it happened to.
            celery_app.send_task(
                "tasks.rolling_period_compose",
                kwargs={"tenant_id": str(tenant_id)},
                queue="sync",
            )
            stats["dispatched"] += 1
        except Exception:
            stats["failed"] += 1
            logger.exception("rolling_period_compose_all.dispatch_failed", extra={"tenant_id": str(tenant_id)})

    stats["reason"] = REASON_DONE if stats["failed"] == 0 else REASON_ERROR
    logger.info("rolling_period_compose_all.completed", extra=dict(stats))
    return stats


@celery_app.task(base=InstrumentedTask, name="tasks.rolling_period_compose_all", queue="sync")
def rolling_period_compose_all():
    """Beat entry point. Opens its own session; logic lives in collect_and_dispatch()."""
    import asyncio

    from app.core.database import worker_async_session

    async def _run() -> dict:
        async with worker_async_session() as db:
            return await collect_and_dispatch(db)

    # No in-task retry: the next daily tick is the retry (house convention).
    return asyncio.run(_run())


@celery_app.task(base=InstrumentedTask, name="tasks.rolling_period_compose", queue="sync")
def rolling_period_compose_tenant(tenant_id: str):
    """Per-tenant sweep. One tenant per task — no cross-tenant session reuse."""
    import asyncio

    from app.core.database import set_tenant_context_session, worker_async_session

    async def _run() -> dict:
        async with worker_async_session() as db:
            # Session-scoped SET, not SET LOCAL: compose_playbook_report commits, and a
            # tool call inside it can commit too (OAuth token refresh via
            # get_valid_token). A transaction-scoped GUC would be cleared by the first
            # such commit and every later RLS query would silently see zero rows. Safe
            # only because this engine is disposable and never returns to an app pool.
            await set_tenant_context_session(db, tenant_id)
            return await sweep_tenant_series(db, uuid.UUID(tenant_id))

    return asyncio.run(_run())
