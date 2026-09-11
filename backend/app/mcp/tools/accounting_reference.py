"""Case-authorized documentation research with tenant-scoped audit provenance."""

from uuid import UUID

from app.mcp.tools.transaction_ops_tools import _authorize, _ToolError
from app.services.transaction_ops.accounting_references import TOPICS, research


async def execute(params, context=None, **kwargs):
    from app.services.audit_service import log_event
    from app.services.transaction_ops.case_service import get_case

    context = context or {}
    if set(params) != {"case_id", "topic"} or not isinstance(params.get("topic"), str) or params["topic"] not in TOPICS:
        return {
            "success": False,
            "error": "Provide an exact case_id and supported reference topic.",
            "supported_topics": list(TOPICS),
        }
    try:
        case_id = UUID(str(params["case_id"]))
        db, tenant_id, actor = await _authorize(context, create=False)
        case = await get_case(db, tenant_id, case_id)
    except (ValueError, PermissionError, _ToolError):
        return {"success": False, "error": "Accounting case or permission unavailable."}
    key = (str(tenant_id), context.get("correlation_id"), str(case.id), params["topic"])
    cache = db.info.setdefault("accounting_reference_results", {})
    if key in cache:
        return {**cache[key], "reused": True}
    budget_key = (str(tenant_id), context.get("correlation_id"))
    used = db.info.setdefault("accounting_reference_budget", {})
    if used.get(budget_key, 0) >= 2:
        return {
            "success": False,
            "error": "Research budget reached. Reuse recorded sources and identify remaining evidence gaps.",
        }
    used[budget_key] = used.get(budget_key, 0) + 1
    result = await research(params["topic"])
    event = await log_event(
        db,
        tenant_id,
        category="transaction_ops",
        action="accounting.reference.observed",
        actor_id=actor.id,
        resource_type="transaction_case",
        resource_id=str(case.id),
        correlation_id=context.get("correlation_id"),
        payload=result,
    )
    output = {"success": True, "case_id": str(case.id), "audit_id": str(event.id), **result}
    cache[key] = output
    return output
