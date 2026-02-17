"""MCP tools for Dev Workspace — read-only browsing + patch proposal.

apply_patch is NOT exposed here; it is REST-only to enforce human approval.
"""

import uuid
from typing import Any

import structlog

logger = structlog.get_logger()


async def execute_list_files(params: dict[str, Any], context: dict[str, Any]) -> dict:
    """List files in a workspace."""
    from app.services import workspace_service as ws_svc

    db = context["db"]
    tenant_id = context["tenant_id"]
    workspace_id = uuid.UUID(params["workspace_id"])
    directory = params.get("directory")
    recursive = params.get("recursive", True)

    files = await ws_svc.list_files(db, workspace_id, uuid.UUID(tenant_id), directory, recursive)
    return {"files": files, "row_count": len(files)}


async def execute_read_file(params: dict[str, Any], context: dict[str, Any]) -> dict:
    """Read a single file from a workspace."""
    from app.services import workspace_service as ws_svc

    db = context["db"]
    tenant_id = context["tenant_id"]
    workspace_id = uuid.UUID(params["workspace_id"])
    file_id = uuid.UUID(params["file_id"])
    line_start = params.get("line_start", 1)
    line_end = params.get("line_end")

    result = await ws_svc.read_file(db, workspace_id, file_id, uuid.UUID(tenant_id), line_start, line_end)
    if not result:
        return {"error": "File not found", "row_count": 0}
    return {**result, "row_count": 1}


async def execute_search(params: dict[str, Any], context: dict[str, Any]) -> dict:
    """Search files by name or content."""
    from app.services import workspace_service as ws_svc

    db = context["db"]
    tenant_id = context["tenant_id"]
    workspace_id = uuid.UUID(params["workspace_id"])
    query = params["query"]
    search_type = params.get("search_type", "filename")
    limit = params.get("limit", 20)

    results = await ws_svc.search_files(db, workspace_id, uuid.UUID(tenant_id), query, search_type, limit)
    return {"results": results, "row_count": len(results)}


async def execute_propose_patch(params: dict[str, Any], context: dict[str, Any]) -> dict:
    """Propose a code change as a unified diff. Creates a draft changeset for human review."""
    from app.services import workspace_service as ws_svc

    db = context["db"]
    tenant_id = context["tenant_id"]
    actor_id = context.get("actor_id")
    workspace_id = uuid.UUID(params["workspace_id"])
    file_path = params["file_path"]
    unified_diff = params["unified_diff"]
    title = params["title"]
    rationale = params.get("rationale")

    proposed_by = uuid.UUID(actor_id) if actor_id else uuid.UUID("00000000-0000-0000-0000-000000000000")

    try:
        result = await ws_svc.propose_patch(
            db,
            workspace_id,
            uuid.UUID(tenant_id),
            file_path,
            unified_diff,
            title,
            proposed_by,
            rationale,
        )
    except ValueError as e:
        return {"error": str(e), "row_count": 0}

    return {**result, "row_count": 1}
