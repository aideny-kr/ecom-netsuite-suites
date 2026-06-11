"""MCP tool: recon.run — trigger a reconciliation run."""

from __future__ import annotations

from datetime import date

import structlog

from app.services.reconciliation.recon_job import ReconJobRunner

logger = structlog.get_logger()


async def execute(params: dict, **kwargs) -> dict:
    """Run a payout reconciliation for the given date range.

    Params:
        date_from: Start date (YYYY-MM-DD)
        date_to: End date (YYYY-MM-DD)
        payout_ids: Optional list of specific payout IDs to reconcile
    """
    # Dispatch boundary — accept BOTH conventions: governed_execute (the only
    # production dispatch) passes everything inside a single ``context=``
    # kwarg; direct callers/tests pass bare ``db=``/``tenant_id=`` kwargs.
    context: dict = kwargs.get("context") or {}
    db = kwargs.get("db") or context.get("db")
    tenant_id = kwargs.get("tenant_id") or context.get("tenant_id")

    if not db or not tenant_id:
        return {"success": False, "error": "Missing database session or tenant context"}

    try:
        date_from = date.fromisoformat(params["date_from"])
        date_to = date.fromisoformat(params["date_to"])
    except (KeyError, ValueError) as e:
        return {"success": False, "error": f"Invalid date parameters: {e}"}

    payout_ids = params.get("payout_ids")

    runner = ReconJobRunner(db=db, tenant_id=str(tenant_id))

    try:
        summary = await runner.run(
            date_from=date_from,
            date_to=date_to,
            payout_ids=payout_ids,
        )

        return {
            "success": True,
            "run_id": summary.run_id,
            "status": summary.status,
            "total_payouts": summary.total_payouts,
            "total_deposits": summary.total_deposits,
            "matched_count": summary.matched_count,
            "exception_count": summary.exception_count,
            "unmatched_count": summary.unmatched_count,
            "total_variance": str(summary.total_variance),
            "match_rate": str(summary.match_rate),
            "message": (
                f"Reconciliation complete: {summary.matched_count} matched, "
                f"{summary.exception_count} exceptions, {summary.unmatched_count} unmatched. "
                f"Match rate: {summary.match_rate}%."
            ),
        }
    except Exception as e:
        logger.error("recon.run.failed", error=str(e))
        return {"success": False, "error": str(e)}
