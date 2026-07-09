"""MCP tool: recon.approve_match — approve a suggested reconciliation match."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reconciliation import ReconciliationResult, ReconciliationRun
from app.services import audit_service
from app.services.reconciliation.four_bucket_classifier import (
    CLOSED_RUN_STATUSES,
    TERMINAL_RESULT_STATUSES,
)


async def execute(params: dict, **kwargs) -> dict:
    """Approve a reconciliation match result.

    This requires confirmation flow — the agent MUST show match details
    and get user confirmation before calling this tool.

    Params:
        result_id: ReconciliationResult ID to approve
    """
    # Dispatch boundary — accept BOTH conventions: governed_execute (the only
    # production dispatch) passes everything inside a single ``context=``
    # kwarg; direct callers/tests pass bare ``db=``/``tenant_id=`` kwargs.
    context: dict = kwargs.get("context") or {}
    db: AsyncSession | None = kwargs.get("db") or context.get("db")
    tenant_id = kwargs.get("tenant_id") or context.get("tenant_id")
    # governed_execute carries the approving user as ``actor_id`` — map it to
    # user_id so chat approvals stamp ``approved_by`` + the per-line audit
    # actor (HITL invariant), never silently NULL.
    user_id = kwargs.get("user_id") or context.get("actor_id")

    if not db or not tenant_id:
        return {"success": False, "error": "Missing database session or tenant context"}

    result_id = params.get("result_id")
    if not result_id:
        return {"success": False, "error": "result_id is required"}
    try:
        result_uuid = uuid.UUID(str(result_id))
    except ValueError:
        # Structured error (same shape as recon.get_exceptions' run_id guard)
        # — an LLM-supplied malformed id must never surface as an uncaught
        # ValueError through the dispatch boundary.
        return {"success": False, "error": f"result_id must be a valid UUID, got: {result_id!r}"}

    stmt = select(ReconciliationResult).where(
        ReconciliationResult.id == result_uuid,
        ReconciliationResult.tenant_id == str(tenant_id),
    )
    result = await db.execute(stmt)
    recon_result = result.scalar_one_or_none()

    if not recon_result:
        return {"success": False, "error": "Result not found"}

    # Close = hard freeze. Guard the run before any status change / audit write so a
    # line left unlocked in a now-closed run cannot be approved post-close.
    # Tenant-scoped: recon_result is already tenant-filtered above, but the run
    # lookup itself must not trust a cross-tenant run id to resolve.
    run = (
        await db.execute(
            select(ReconciliationRun).where(
                ReconciliationRun.id == recon_result.run_id,
                ReconciliationRun.tenant_id == str(tenant_id),
            )
        )
    ).scalar_one_or_none()
    if run is not None and run.status in CLOSED_RUN_STATUSES:
        return {"success": False, "error": "Period is closed — cannot modify"}

    if recon_result.status == "approved":
        return {"success": False, "error": "Already approved"}

    if recon_result.status == "locked":
        return {"success": False, "error": "Period is locked — cannot modify"}

    # Any other terminal status (rejected, carried_forward) must not be flipped to
    # approved either — e.g. a carried_forward result approved here would then get
    # LOCKED at close, violating the carried_forward-never-locks invariant. Mirrors
    # the REST single-approve endpoint's guard (app/api/v1/reconciliation.py).
    if recon_result.status in TERMINAL_RESULT_STATUSES:
        return {
            "success": False,
            "error": f"Result cannot be approved (status={recon_result.status})",
        }

    recon_result.status = "approved"
    recon_result.approved_by = uuid.UUID(str(user_id)) if user_id else None
    recon_result.approved_at = datetime.now(timezone.utc)

    await audit_service.log_event(
        db=db,
        tenant_id=uuid.UUID(str(tenant_id)),
        category="reconciliation",
        action="recon.approve",
        actor_id=uuid.UUID(str(user_id)) if user_id else None,
        actor_type="user",
        resource_type="reconciliation_result",
        resource_id=result_id,
    )
    await db.commit()

    return {
        "success": True,
        "result_id": result_id,
        "status": "approved",
        "message": "Match approved successfully.",
    }
