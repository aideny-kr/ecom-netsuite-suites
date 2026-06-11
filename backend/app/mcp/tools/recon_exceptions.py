"""MCP tool: recon.get_exceptions — fetch the authoritative needs_review bucket."""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reconciliation import ReconciliationResult
from app.services.reconciliation.four_bucket_classifier import BUCKET_NEEDS_REVIEW

# Already-dispositioned rows are not open exceptions.
_DISPOSITIONED_STATUSES = ("approved", "locked")


async def execute(params: dict, **kwargs) -> dict:
    """Fetch open exceptions for a run — the authoritative ``needs_review`` bucket.

    Exceptions = rows the four-bucket classifier placed in ``needs_review``
    (unmatched + material-variance rows), excluding already-dispositioned rows
    (status approved/locked). Each row carries the authoritative disposition
    fields ``status`` and ``bucket``; ``advisory_match_score`` is the advisory
    match composite — informational only, NEVER a verdict. Disposition always
    derives from ``status``/``bucket``, never from the advisory score.

    Params:
        run_id: Reconciliation run ID
        min_variance: Optional minimum absolute variance amount to include
            (Decimal-safe; e.g. "50.00")
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
            # Authoritative selection — the four-bucket classification, never
            # the advisory confidence composite (decoupling pattern).
            ReconciliationResult.bucket == BUCKET_NEEDS_REVIEW,
            ReconciliationResult.status.not_in(_DISPOSITIONED_STATUSES),
        )
        .order_by(ReconciliationResult.variance_amount.desc())
        .limit(50)
    )

    min_variance = params.get("min_variance")
    if min_variance is not None:
        try:
            min_variance_dec = Decimal(str(min_variance))
        except InvalidOperation:
            return {"success": False, "error": f"min_variance must be numeric, got: {min_variance!r}"}
        stmt = stmt.where(func.abs(ReconciliationResult.variance_amount) >= min_variance_dec)

    result = await db.execute(stmt)
    rows = result.scalars().all()

    exceptions = []
    for r in rows:
        # Strip confidence_signals (calibration instrumentation, not
        # investigative — and a no-LLM-numbers surface) via a filtered COPY:
        # popping r.evidence in place would dirty the session and could
        # persist the deletion.
        evidence = {k: v for k, v in r.evidence.items() if k != "confidence_signals"} if r.evidence else r.evidence
        exceptions.append(
            {
                "result_id": str(r.id),
                "match_type": r.match_type,
                # Authoritative disposition — what the row IS.
                "status": r.status,
                "bucket": r.bucket,
                # Advisory composite — informational only, never a verdict.
                "advisory_match_score": str(r.confidence),
                "stripe_amount": str(r.stripe_amount) if r.stripe_amount else None,
                "netsuite_amount": str(r.netsuite_amount) if r.netsuite_amount else None,
                "variance_amount": str(r.variance_amount),
                "variance_type": r.variance_type,
                "variance_explanation": r.variance_explanation,
                "currency": r.currency,
                "evidence": evidence,
            }
        )

    return {
        "success": True,
        "run_id": run_id,
        "exception_count": len(exceptions),
        "exceptions": exceptions,
    }
