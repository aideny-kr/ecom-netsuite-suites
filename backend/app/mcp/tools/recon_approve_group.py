"""MCP tool: recon.approve_group — HITL-gated bulk approve of a resolution
group.

NEVER approves directly through chat streaming: ``base_agent.py`` intercepts
the sanitized tool-use block name (``recon_approve_group``) BEFORE this
``execute()`` runs and yields a confirmation card instead (see the
"Recon group-approve HITL intercept" block). This ``execute()`` is only ever
reached via the POST-confirmation path — the chat orchestrator's
``validate_and_extract_confirmation`` → ``execute_tool_call`` — after the
human has explicitly approved. It shares the EXACT same behavior as the REST
``.../resolution-groups/{group_key}/approve`` endpoint via
``approve_group_core`` (Task 6) — no re-implementation of the
eligibility/audit invariants (UI-flag gate, run-open guard, materiality
opt-in, per-line + bulk audit).
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException

from app.services.reconciliation.group_actions import approve_group_core


async def execute(params: dict, **kwargs) -> dict:
    """Approve a resolution group (post-HITL-confirmation only).

    Params:
        run_id: Reconciliation run ID
        group_key: "root_cause:action:booking_vehicle"
        currency: Optional currency scope (multi-currency group_key collisions)
        notes: Optional free-text note recorded on the bulk audit event
        included_above_materiality_ids: Optional list of above-materiality
            proposal IDs to explicitly include (above-materiality proposals
            never approve implicitly)
        excluded_ids: Optional list of proposal IDs to exclude from this batch
    """
    # Dispatch boundary — accept BOTH conventions (mirrors recon.approve_match):
    # the ONLY production caller (chat → mcp_server.call_tool → governed_execute)
    # passes everything inside a single ``context=`` kwarg, while direct callers
    # and tests pass bare ``db=``/``tenant_id=``/``actor_id=`` kwargs. The
    # approving user arrives as ``actor_id`` in the governed-dispatch context.
    context: dict = kwargs.get("context") or {}
    db = kwargs.get("db") or context.get("db")
    tenant_id = kwargs.get("tenant_id") or context.get("tenant_id")
    actor_id = kwargs.get("actor_id") or context.get("actor_id")

    if not db or not tenant_id:
        return {"success": False, "error": "Missing database session or tenant context"}

    run_id = params.get("run_id")
    group_key = params.get("group_key")
    if not run_id:
        return {"success": False, "error": "run_id is required"}
    if not group_key:
        return {"success": False, "error": "group_key is required"}

    try:
        result = await approve_group_core(
            db,
            tenant_id=uuid.UUID(str(tenant_id)),
            actor_id=uuid.UUID(str(actor_id)) if actor_id else None,
            run_id=str(run_id),
            group_key=str(group_key),
            notes=params.get("notes"),
            included_above_materiality_ids=params.get("included_above_materiality_ids") or [],
            excluded_ids=params.get("excluded_ids") or [],
            currency=params.get("currency"),
        )
    except HTTPException as exc:
        # Structured error — an LLM-facing tool result must never surface a
        # raw HTTPException through the dispatch boundary.
        return {"success": False, "error": exc.detail}

    return {
        "success": True,
        "approved_count": result.approved_count,
        "skipped_count": result.skipped_count,
        "correlation_id": result.correlation_id,
    }
