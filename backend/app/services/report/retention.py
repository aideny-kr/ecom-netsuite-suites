"""Report version retention — Slice C of live-dashboard reports (spec §6.2).

Caps stored ``report_versions`` rows per report (default 30, settings-backed):
``pinned`` rows are exempt (an auditor's pin outranks the janitor, so the total may
legitimately sit above the cap), the CURRENT version is never pruned (structurally the
parent mirrors it; the guard is explicit anyway), and the oldest unpinned rows go
first. Runs at the single point of version production — ``refresh_report``,
post-publish — as a best-effort janitor whose failure must never fail the publish.

Caller owns transaction + tenant context: RLS scopes every statement here, with an
explicit tenant_id predicate as defense-in-depth.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.report import Report
from app.models.report_version import ReportVersion

logger = logging.getLogger(__name__)


async def enforce_version_retention(
    db: AsyncSession,
    *,
    report_id: uuid.UUID,
    tenant_id: uuid.UUID,
    cap: int | None = None,
) -> int:
    """Prune oldest-unpinned versions of ``report_id`` down to ``cap``; returns the
    number pruned. Never deletes pinned rows or the row matching the parent's current
    version — with enough pinned rows the total stays above the cap by design."""
    if cap is None:
        cap = settings.REPORT_VERSION_RETENTION_CAP
    total = (
        await db.execute(
            select(func.count())
            .select_from(ReportVersion)
            .where(ReportVersion.report_id == report_id, ReportVersion.tenant_id == tenant_id)
        )
    ).scalar_one()
    excess = total - cap
    if excess <= 0:
        return 0
    current_version = (
        await db.execute(select(Report.version).where(Report.id == report_id, Report.tenant_id == tenant_id))
    ).scalar_one()
    victims = (
        select(ReportVersion.id)
        .where(
            ReportVersion.report_id == report_id,
            ReportVersion.tenant_id == tenant_id,
            ReportVersion.pinned.is_(False),
            ReportVersion.version != current_version,
        )
        .order_by(ReportVersion.version.asc())
        .limit(excess)
        .scalar_subquery()
    )
    result = await db.execute(delete(ReportVersion).where(ReportVersion.id.in_(victims)))
    pruned = result.rowcount or 0
    if pruned:
        logger.info("report version retention pruned %d of %d (cap %d) for report %s", pruned, total, cap, report_id)
    return pruned
