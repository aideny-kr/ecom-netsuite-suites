"""MCP tool: recon.get_exceptions — fetch the authoritative needs_review bucket."""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reconciliation import ReconciliationResult
from app.services.reconciliation.four_bucket_classifier import (
    BUCKET_NEEDS_REVIEW,
    bucket_conditions,
)

# Hard cap on returned rows (largest |variance| first). ``exception_count``
# stays the TRUE filtered total via a separate count query — the no-LLM-numbers
# framing promises the authoritative bucket size, never a capped len().
_MAX_ROWS = 50

# Already-dispositioned rows are not open exceptions. ``rejected`` deliberately
# stays VISIBLE here even though bulk-approve's ``_SKIP_STATUSES``
# (app/api/v1/reconciliation.py) excludes it from re-approval: a rejected match
# is a human disposition of the MATCH, not a resolution of the money — the row
# still needs investigation, so it remains an open exception.
_DISPOSITIONED_STATUSES = ("approved", "locked")


def _evidence_for_llm(evidence: dict | None) -> dict | None:
    """Filtered COPY of stored evidence for the LLM-facing payload.

    Strips ``confidence_signals`` — calibration instrumentation, not
    investigative, and this is a no-LLM-numbers surface (raw sub-scores would
    invite the model to recite/round them). Never mutates the ORM dict in
    place: popping would dirty the session and could persist the deletion.
    Guards the common no-signals case (no pointless copy).
    """
    if not evidence or "confidence_signals" not in evidence:
        return evidence
    return {k: v for k, v in evidence.items() if k != "confidence_signals"}


async def execute(params: dict, **kwargs) -> dict:
    """Fetch open exceptions for a run — the authoritative ``needs_review`` bucket.

    Exceptions = rows the four-bucket classifier placed in ``needs_review``
    (unmatched + material-variance rows), excluding already-dispositioned rows
    (status approved/locked). Returns at most 50 rows, largest ABSOLUTE
    variance first; ``exception_count`` is the TRUE total matching the filters
    (``returned`` / ``truncated`` report the cap). Each row carries the
    authoritative disposition fields ``status`` and ``bucket``;
    ``advisory_match_score`` is the advisory match composite — informational
    only, NEVER a verdict. Disposition always derives from
    ``status``/``bucket``, never from the advisory score.

    Params:
        run_id: Reconciliation run ID
        min_variance: Optional minimum absolute variance amount to include
            (Decimal-safe and finite; e.g. "50.00")
    """
    db: AsyncSession | None = kwargs.get("db")
    tenant_id = kwargs.get("tenant_id")

    if not db or not tenant_id:
        return {"success": False, "error": "Missing database session or tenant context"}

    run_id = params.get("run_id")
    if not run_id:
        return {"success": False, "error": "run_id is required"}

    filters = [
        ReconciliationResult.tenant_id == str(tenant_id),
        ReconciliationResult.run_id == uuid.UUID(run_id),
        # Authoritative selection — the canonical four-bucket SQL twin, never
        # the advisory confidence composite (decoupling pattern).
        bucket_conditions(BUCKET_NEEDS_REVIEW),
        ReconciliationResult.status.not_in(_DISPOSITIONED_STATUSES),
    ]

    min_variance = params.get("min_variance")
    if min_variance is not None:
        try:
            min_variance_dec = Decimal(str(min_variance))
        except InvalidOperation:
            return {"success": False, "error": f"min_variance must be numeric, got: {min_variance!r}"}
        # NaN/Infinity/sNaN parse as valid Decimals but silently match ZERO
        # rows at the SQL layer — the tool would report "no exceptions" for a
        # run full of them. Reject non-finite values up front.
        if not min_variance_dec.is_finite():
            return {"success": False, "error": f"min_variance must be a finite number, got: {min_variance!r}"}
        filters.append(func.abs(ReconciliationResult.variance_amount) >= min_variance_dec)

    # TRUE total over the SAME filters, before the row cap (count honesty).
    total = (await db.execute(select(func.count(ReconciliationResult.id)).where(*filters))).scalar_one()

    stmt = (
        select(ReconciliationResult)
        .where(*filters)
        # Largest ABSOLUTE variance first: signed desc would sort
        # negative-variance rows (refund-heavy payouts) dead-last and truncate
        # them at the cap — consistent with the abs-based min_variance filter.
        .order_by(func.abs(ReconciliationResult.variance_amount).desc())
        .limit(_MAX_ROWS)
    )
    rows = (await db.execute(stmt)).scalars().all()

    exceptions = []
    for r in rows:
        exceptions.append(
            {
                "result_id": str(r.id),
                "match_type": r.match_type,
                # Authoritative disposition — what the row IS.
                "status": r.status,
                "bucket": r.bucket,
                # Advisory composite — informational only, never a verdict.
                "advisory_match_score": str(r.confidence),
                # ``is not None``: a genuine Decimal("0.00") is falsy and must
                # serialize as "0.00", not null.
                "stripe_amount": str(r.stripe_amount) if r.stripe_amount is not None else None,
                "netsuite_amount": str(r.netsuite_amount) if r.netsuite_amount is not None else None,
                "variance_amount": str(r.variance_amount),
                "variance_type": r.variance_type,
                "variance_explanation": r.variance_explanation,
                "currency": r.currency,
                "evidence": _evidence_for_llm(r.evidence),
            }
        )

    return {
        "success": True,
        "run_id": run_id,
        "exception_count": total,
        "returned": len(exceptions),
        "truncated": total > len(exceptions),
        "exceptions": exceptions,
    }
