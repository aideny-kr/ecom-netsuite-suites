"""MCP tool: recon.get_resolution_summary — summary-first read for chat.

Read-only twin of the ``/runs/{run_id}/resolution-summary`` REST endpoint
(``app.api.v1.reconciliation.get_resolution_summary``): same GROUP BY on real
columns (never parsed out of ``group_key``), same live-proposal filter
(superseded/rejected excluded). Honest framing (mirrors recon.get_exceptions):
recon tools have no ``tool_categories._EXACT`` entry, so there is no
``data_table`` SSE interception — the description instructs the model to
transcribe every number VERBATIM, never recompute/round/sum/paraphrase.
Groups are capped at ``_MAX_GROUPS``, largest amount first; ``group_count``
is the TRUE total distinct-group count and ``truncated`` reports whether the
returned list was cut off — never present a truncated list as exhaustive.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reconciliation import ReconciliationResult, ReconciliationRun, ReconResolutionProposal

# Hard cap on returned groups (largest total amount first). Module-level so
# tests can monkeypatch it to exercise the truncation path without seeding
# 20+ groups.
_MAX_GROUPS = 20


def _pct(numerator: int, denominator: int) -> Decimal:
    if denominator == 0:
        return Decimal("0")
    return (Decimal(numerator) / Decimal(denominator) * 100).quantize(Decimal("0.1"))


async def execute(params: dict, **kwargs) -> dict:
    """Fetch the summary-first resolution report for a run.

    Params:
        run_id: Reconciliation run ID

    Returns rates + a capped, amount-desc list of resolution groups. Every
    amount is a ``str()`` (Decimal-safe). Transcribe every number VERBATIM —
    never recompute, round, sum, or paraphrase amounts in prose — and quote
    ``group_count``/``proposals_count`` exactly.
    """
    # Dispatch boundary — accept BOTH conventions (mirrors recon.get_exceptions):
    # the ONLY production caller (chat → mcp_server.call_tool → governed_execute)
    # passes everything inside a single ``context=`` kwarg, while direct callers
    # and tests pass bare ``db=``/``tenant_id=`` kwargs.
    context: dict = kwargs.get("context") or {}
    db: AsyncSession | None = kwargs.get("db") or context.get("db")
    tenant_id = kwargs.get("tenant_id") or context.get("tenant_id")

    if not db or not tenant_id:
        return {"success": False, "error": "Missing database session or tenant context"}

    run_id = params.get("run_id")
    if not run_id:
        return {"success": False, "error": "run_id is required"}
    try:
        run_uuid = uuid.UUID(str(run_id))
    except ValueError:
        return {"success": False, "error": f"run_id must be a valid UUID, got: {run_id!r}"}

    run = (
        await db.execute(
            select(ReconciliationRun).where(
                ReconciliationRun.id == run_uuid,
                ReconciliationRun.tenant_id == str(tenant_id),
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return {"success": False, "error": "Run not found"}

    total_results = (
        await db.execute(
            select(func.count(ReconciliationResult.id)).where(
                ReconciliationResult.run_id == run_uuid,
                ReconciliationResult.tenant_id == str(tenant_id),
            )
        )
    ).scalar_one()
    matches_count = run.matches_count

    P = ReconResolutionProposal
    live = (
        P.run_id == run_uuid,
        P.tenant_id == str(tenant_id),
        P.status.notin_(("superseded", "rejected")),
    )

    group_rows = (
        await db.execute(
            select(
                P.root_cause,
                P.action,
                P.booking_vehicle,
                P.group_key,
                P.currency,
                func.count(P.id).label("count"),
                func.count(P.id).filter(P.status == "proposed").label("proposed_count"),
                func.count(P.id).filter(P.status == "approved").label("approved_count"),
                func.coalesce(func.sum(P.proposed_amount), 0).label("total_amount"),
                func.count(P.id)
                .filter(P.above_materiality.is_(True), P.status == "proposed")
                .label("above_materiality_count"),
            )
            .where(*live)
            .group_by(P.root_cause, P.action, P.booking_vehicle, P.group_key, P.currency)
            .order_by(func.coalesce(func.sum(P.proposed_amount), 0).desc())
        )
    ).all()

    all_groups = [
        {
            "group_key": r.group_key,
            "currency": r.currency,
            "root_cause": r.root_cause,
            "action": r.action,
            "booking_vehicle": r.booking_vehicle,
            "count": r.count,
            "proposed_count": r.proposed_count,
            "approved_count": r.approved_count,
            "total_amount": str(r.total_amount),
            "above_materiality_count": r.above_materiality_count,
        }
        for r in group_rows
    ]

    proposals_count = sum(g["count"] for g in all_groups)
    explained_count = sum(g["count"] for g in all_groups if g["action"] != "needs_human")

    group_count = len(all_groups)
    groups = all_groups[:_MAX_GROUPS]

    return {
        "success": True,
        "run_id": run_id,
        "match_rate": str(_pct(matches_count, total_results)),
        "explained_rate": str(_pct(explained_count, proposals_count)),
        "proposals_count": proposals_count,
        "groups": groups,
        "group_count": group_count,
        "truncated": group_count > len(groups),
    }
