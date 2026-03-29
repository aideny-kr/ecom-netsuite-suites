"""MCP tool: recon.approve_match — approve a suggested reconciliation match."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reconciliation import ReconciliationResult


async def execute(params: dict, **kwargs) -> dict:
    """Approve a reconciliation match result.

    This requires confirmation flow — the agent MUST show match details
    and get user confirmation before calling this tool.

    Params:
        result_id: ReconciliationResult ID to approve
    """
    db: AsyncSession | None = kwargs.get("db")
    tenant_id = kwargs.get("tenant_id")
    user_id = kwargs.get("user_id")

    if not db or not tenant_id:
        return {"success": False, "error": "Missing database session or tenant context"}

    result_id = params.get("result_id")
    if not result_id:
        return {"success": False, "error": "result_id is required"}

    stmt = select(ReconciliationResult).where(
        ReconciliationResult.id == uuid.UUID(result_id),
        ReconciliationResult.tenant_id == str(tenant_id),
    )
    result = await db.execute(stmt)
    recon_result = result.scalar_one_or_none()

    if not recon_result:
        return {"success": False, "error": "Result not found"}

    if recon_result.status == "approved":
        return {"success": False, "error": "Already approved"}

    if recon_result.status == "locked":
        return {"success": False, "error": "Period is locked — cannot modify"}

    recon_result.status = "approved"
    recon_result.approved_by = uuid.UUID(str(user_id)) if user_id else None
    recon_result.approved_at = datetime.now(timezone.utc)

    await db.commit()

    return {
        "success": True,
        "result_id": result_id,
        "status": "approved",
        "message": "Match approved successfully.",
    }
