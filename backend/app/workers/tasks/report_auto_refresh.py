"""Beat auto-refresh sweep for live-dashboard reports — Slice C (spec §4C/§6.1).

Per-tenant sweep: finds due recipe-bearing reports and replays each through the
existing ``refresh_report`` with the system actor (``actor_id=None,
actor_type="system"``). One tenant per task — cross-tenant context leakage is
impossible by construction (``reports`` is FORCE-RLS; there is no cross-tenant read).

The sweep owns the FAILURE LADDER — launch-critical with daily-by-default, because
the known NetSuite single-use-refresh-token death would otherwise retry-storm a dead
OAuth connection forever:

- failure → ``refresh_failure_count`` += 1; the report keeps its last good version
  (refresh_report never corrupts current) and the FE shows a staleness banner;
- ``hourly`` behaves as ``daily`` at >= HOURLY_BACKOFF_THRESHOLD consecutive failures
  (derived — the user's chosen interval is never overwritten);
- >= PAUSE_THRESHOLD consecutive failures → ``auto_refresh_paused_at`` stamped
  (audited, system actor) and the report leaves the sweep until the user's explicit
  one-click resume — a later success never un-pauses;
- success → count resets to 0. Debounce (429) and supersede mean "someone else
  refreshed just now" — never ladder increments (a manual-refresh race must not walk
  a healthy report toward pause).

Manual refresh keeps its own anti-storm (the attempt-time debounce stamp); this
module never touches it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import set_tenant_context
from app.models.report import Report
from app.services import audit_service
from app.services.report.refresh_service import (
    RefreshDebouncedError,
    RefreshError,
    RefreshSupersededError,
    refresh_report,
)

logger = logging.getLogger(__name__)

HOURLY_BACKOFF_THRESHOLD = 3  # hourly behaves as daily from this many consecutive failures
PAUSE_THRESHOLD = 7  # spec §4C "~7 consecutive failed refreshes" → pause
# The refresh stamp is written seconds AFTER the Beat tick fires, so at the next tick
# the elapsed time is fractionally UNDER the nominal interval — without slack an
# hourly report would skip every other tick and a daily one would slip a day.
DUE_SLACK_SECONDS = 300
_HOURLY_SECONDS = 3600
_DAILY_SECONDS = 86400


def _effective_interval_seconds(auto_refresh: str, failure_count: int) -> int:
    """The BACKOFF is derived here, never stored — reports.auto_refresh always holds
    the user's choice. Anything not recognizably hourly is treated as daily."""
    if auto_refresh == "hourly" and failure_count < HOURLY_BACKOFF_THRESHOLD:
        return _HOURLY_SECONDS
    return _DAILY_SECONDS


async def _reset_ladder(db: AsyncSession, tenant_id: uuid.UUID, report_id: uuid.UUID) -> None:
    # refresh_report's publish commit cleared any SET LOCAL context — re-establish.
    await set_tenant_context(db, str(tenant_id))
    row = (await db.execute(select(Report).where(Report.id == report_id))).scalar_one_or_none()
    if row is not None and row.refresh_failure_count:
        row.refresh_failure_count = 0
        await db.commit()


async def _record_failure(
    db: AsyncSession, tenant_id: uuid.UUID, report_id: uuid.UUID, *, now: datetime, detail: str
) -> bool:
    """Increment the ladder; pause at the threshold. Returns True when this failure
    paused the report."""
    # refresh_report's failure path rolled back (and committed a failure audit) —
    # the GUC is gone and its ORM instances are expired; re-set + re-select.
    await set_tenant_context(db, str(tenant_id))
    row = (await db.execute(select(Report).where(Report.id == report_id))).scalar_one_or_none()
    if row is None:  # deleted mid-sweep — nothing to record
        return False
    row.refresh_failure_count += 1
    paused = False
    if row.refresh_failure_count >= PAUSE_THRESHOLD and row.auto_refresh_paused_at is None:
        row.auto_refresh_paused_at = now
        paused = True
        await audit_service.log_event(
            db=db,
            tenant_id=tenant_id,
            category="report",
            action="report.auto_refresh_paused",
            actor_id=None,
            actor_type="system",
            resource_type="report",
            resource_id=str(report_id),
            payload={"failure_count": row.refresh_failure_count, "detail": (detail or "")[:200]},
        )
    await db.commit()
    return paused


async def sweep_tenant_reports(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    now: datetime | None = None,
    batch: int | None = None,
) -> dict:
    """Refresh the tenant's due reports (most-stale first, at most ``batch``); returns
    the stats dict the jobs table records. Self-sufficient re: tenant context — it
    re-establishes SET LOCAL around every statement group, so it is safe on both the
    worker's session-SET engine and a plain test session."""
    now = now or datetime.now(timezone.utc)
    batch = settings.REPORT_AUTO_REFRESH_BATCH if batch is None else batch
    stats = {"tenant_id": str(tenant_id), "due": 0, "refreshed": 0, "failed": 0, "skipped": 0, "paused": 0}

    await set_tenant_context(db, str(tenant_id))
    # Plain tuples, not ORM instances: refresh_report's failure path expires this
    # session's instances, so the loop must not hold attribute-loaded rows.
    candidates = (
        await db.execute(
            select(Report.id, Report.auto_refresh, Report.refresh_failure_count, Report.last_refreshed_at)
            .where(
                Report.tenant_id == tenant_id,
                # Snapshot-only rows are inert (§6.1) — and they are NOT all SQL NULL:
                # compose passes recipe_json=None explicitly (report_export.py) and the
                # ORM's JSONB none_as_null=False default persists that as jsonb 'null'.
                # jsonb_typeof excludes both (SQL NULL → NULL, jsonb null → 'null').
                func.jsonb_typeof(Report.recipe_json) == "object",
                Report.auto_refresh != "off",
                Report.auto_refresh_paused_at.is_(None),
            )
            .order_by(Report.last_refreshed_at.asc().nulls_first())
        )
    ).all()

    due = [
        c
        for c in candidates
        if c.last_refreshed_at is None
        or (now - c.last_refreshed_at).total_seconds()
        >= _effective_interval_seconds(c.auto_refresh, c.refresh_failure_count) - DUE_SLACK_SECONDS
    ]
    stats["due"] = len(due)

    for c in due[:batch]:
        try:
            await refresh_report(db, report_id=c.id, tenant_id=tenant_id, actor_id=None, actor_type="system")
            stats["refreshed"] += 1
            outcome = _reset_ladder
        except (RefreshDebouncedError, RefreshSupersededError):
            # someone else refreshed just now — not a failure, the ladder must not move
            stats["skipped"] += 1
            continue
        except RefreshError as exc:
            stats["failed"] += 1
            logger.warning(
                "report auto-refresh failed", extra={"report_id": str(c.id), "detail": exc.detail}
            )

            async def outcome(db, tenant_id, report_id, *, _detail=exc.detail, _now=now):
                if await _record_failure(db, tenant_id, report_id, now=_now, detail=_detail):
                    stats["paused"] += 1

        # Ladder bookkeeping is best-effort per report: a broken row (e.g. deleted
        # mid-sweep) must not abort the rest of the tenant's batch.
        try:
            await outcome(db, tenant_id, c.id)
        except Exception:
            logger.warning("report auto-refresh ladder write failed", exc_info=True)
            await db.rollback()

    if stats["due"]:
        logger.info("report auto-refresh sweep completed", extra=stats)
    return stats