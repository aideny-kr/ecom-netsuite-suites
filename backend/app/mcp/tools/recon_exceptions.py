"""MCP tool: recon.get_exceptions — fetch unmatched/low-confidence results."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reconciliation import ReconciliationResult


async def execute(params: dict, **kwargs) -> dict:
    """Fetch exception results (unmatched + low-confidence) for a run.

    Params:
        run_id: Reconciliation run ID
        min_variance: Optional minimum variance amount to filter (default: 0)
    """
    db: AsyncSession | None = kwargs.get("db")
    tenant_id = kwargs.get("tenant_id")

    if not db or not tenant_id:
        return {"success": False, "error": "Missing database session or tenant context"}

    run_id = params.get("run_id")
    if not run_id:
        return {"success": False, "error": "run_id is required"}

    stmt = (
        select(ReconciliationResult)
        .where(
            ReconciliationResult.tenant_id == str(tenant_id),
            ReconciliationResult.run_id == uuid.UUID(run_id),
            ReconciliationResult.status.in_(["pending", "suggested"]),
        )
        .order_by(ReconciliationResult.variance_amount.desc())
        .limit(50)
    )

    result = await db.execute(stmt)
    rows = result.scalars().all()

    exceptions = []
    for r in rows:
        exceptions.append(
            {
                "result_id": str(r.id),
                "match_type": r.match_type,
                "confidence": str(r.confidence),
                "stripe_amount": str(r.stripe_amount) if r.stripe_amount else None,
                "netsuite_amount": str(r.netsuite_amount) if r.netsuite_amount else None,
                "variance_amount": str(r.variance_amount),
                "variance_type": r.variance_type,
                "variance_explanation": r.variance_explanation,
                "currency": r.currency,
                "evidence": r.evidence,
            }
        )

    return {
        "success": True,
        "run_id": run_id,
        "exception_count": len(exceptions),
        "exceptions": exceptions,
    }
