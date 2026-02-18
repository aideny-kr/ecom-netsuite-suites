"""MCP tools for Dev Workspace — browsing, patch proposal, and patch application."""

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


async def execute_apply_patch(params: dict[str, Any], context: dict[str, Any]) -> dict:
    """Apply an approved changeset. Requires workspace.apply permission."""
    from app.core.dependencies import has_permission
    from app.services import workspace_service as ws_svc

    db = context["db"]
    tenant_id = context["tenant_id"]
    actor_id = context.get("actor_id")
    changeset_id = uuid.UUID(params["changeset_id"])
    actor_uuid = uuid.UUID(actor_id) if actor_id else None

    if actor_uuid is None:
        return {"error": "Actor ID required for apply", "row_count": 0}

    allowed = await has_permission(db, actor_uuid, "workspace.apply")
    if not allowed:
        return {"error": "Permission denied: workspace.apply required", "row_count": 0}

    try:
        cs = await ws_svc.apply_changeset(db, changeset_id, uuid.UUID(tenant_id), actor_uuid)
    except ValueError as e:
        return {"error": str(e), "row_count": 0}

    return {
        "changeset_id": str(cs.id),
        "status": cs.status,
        "applied_by": str(cs.applied_by),
        "applied_at": cs.applied_at.isoformat() if cs.applied_at else None,
        "row_count": 1,
    }


async def execute_run_suiteql_assertions(params: dict[str, Any], context: dict[str, Any]) -> dict:
    """Trigger SuiteQL assertion run against a changeset. Requires workspace.manage permission."""
    from app.core.dependencies import has_permission
    from app.services import audit_service, runner_service
    from app.services import workspace_service as ws_svc
    from app.services.assertion_service import validate_assertions

    db = context["db"]
    tenant_id = context["tenant_id"]
    actor_id = context.get("actor_id")
    actor_uuid = uuid.UUID(actor_id) if actor_id else None
    if actor_uuid is None:
        return {"error": "Actor ID required for assertion run", "row_count": 0}

    allowed = await has_permission(db, actor_uuid, "workspace.manage")
    if not allowed:
        return {"error": "Permission denied: workspace.manage required", "row_count": 0}

    changeset_raw = params.get("changeset_id")
    if not changeset_raw:
        return {"error": "changeset_id is required", "row_count": 0}
    changeset_id = uuid.UUID(changeset_raw)

    assertions = params.get("assertions", [])
    if not assertions:
        return {"error": "At least one assertion is required", "row_count": 0}

    try:
        validate_assertions(assertions)
    except ValueError as e:
        return {"error": str(e), "row_count": 0}

    cs = await ws_svc.get_changeset(db, changeset_id, uuid.UUID(tenant_id))
    if cs is None:
        return {"error": "Changeset not found", "row_count": 0}
    if cs.status != "approved":
        return {"error": f"Changeset must be approved (current: {cs.status})", "row_count": 0}

    run = await runner_service.create_run(
        db,
        tenant_id=uuid.UUID(tenant_id),
        workspace_id=cs.workspace_id,
        run_type="suiteql_assertions",
        triggered_by=actor_uuid,
        changeset_id=changeset_id,
    )
    await audit_service.log_event(
        db=db,
        tenant_id=uuid.UUID(tenant_id),
        category="workspace",
        action="workspace.run.triggered",
        actor_id=actor_uuid,
        resource_type="workspace_run",
        resource_id=str(run.id),
        correlation_id=run.correlation_id,
        payload={"run_type": "suiteql_assertions", "changeset_id": str(changeset_id)},
    )
    await db.flush()

    from app.workers.tasks.workspace_run import workspace_run_task

    workspace_run_task.delay(
        tenant_id=tenant_id,
        run_id=str(run.id),
        correlation_id=run.correlation_id,
        extra_params={"assertions": assertions},
    )

    return {"run_id": str(run.id), "status": run.status, "row_count": 1}


async def execute_deploy_sandbox(params: dict[str, Any], context: dict[str, Any]) -> dict:
    """Trigger gated sandbox deploy. Requires workspace.manage permission + prerequisites."""
    from app.core.dependencies import has_permission
    from app.services import audit_service, runner_service
    from app.services import workspace_service as ws_svc
    from app.services.deploy_service import check_deploy_prerequisites

    db = context["db"]
    tenant_id = context["tenant_id"]
    actor_id = context.get("actor_id")
    actor_uuid = uuid.UUID(actor_id) if actor_id else None
    if actor_uuid is None:
        return {"error": "Actor ID required for deploy", "row_count": 0}

    allowed = await has_permission(db, actor_uuid, "workspace.manage")
    if not allowed:
        return {"error": "Permission denied: workspace.manage required", "row_count": 0}

    changeset_raw = params.get("changeset_id")
    if not changeset_raw:
        return {"error": "changeset_id is required", "row_count": 0}
    changeset_id = uuid.UUID(changeset_raw)
    sandbox_id = params.get("sandbox_id")
    if not sandbox_id:
        return {"error": "sandbox_id is required", "row_count": 0}

    cs = await ws_svc.get_changeset(db, changeset_id, uuid.UUID(tenant_id))
    if cs is None:
        return {"error": "Changeset not found", "row_count": 0}
    if cs.status != "approved":
        return {"error": f"Changeset must be approved (current: {cs.status})", "row_count": 0}

    override_reason = params.get("override_reason")
    require_assertions = params.get("require_assertions", False)

    gate_result = await check_deploy_prerequisites(
        db,
        changeset_id,
        uuid.UUID(tenant_id),
        require_assertions=require_assertions,
        override_reason=override_reason,
    )

    if not gate_result["allowed"]:
        return {"error": gate_result["blocked_reason"], "row_count": 0}

    if gate_result["override"]["applied"]:
        await audit_service.log_event(
            db=db,
            tenant_id=uuid.UUID(tenant_id),
            category="workspace",
            action="deploy.gate_override",
            actor_id=actor_uuid,
            resource_type="changeset",
            resource_id=str(changeset_id),
            payload={
                "sandbox_id": sandbox_id,
                "override_reason": override_reason,
                "gates": gate_result["gates"],
            },
        )

    run = await runner_service.create_run(
        db,
        tenant_id=uuid.UUID(tenant_id),
        workspace_id=cs.workspace_id,
        run_type="deploy_sandbox",
        triggered_by=actor_uuid,
        changeset_id=changeset_id,
    )
    await audit_service.log_event(
        db=db,
        tenant_id=uuid.UUID(tenant_id),
        category="workspace",
        action="workspace.run.triggered",
        actor_id=actor_uuid,
        resource_type="workspace_run",
        resource_id=str(run.id),
        correlation_id=run.correlation_id,
        payload={
            "run_type": "deploy_sandbox",
            "changeset_id": str(changeset_id),
            "sandbox_id": sandbox_id,
            "gates": gate_result["gates"],
            "override": gate_result["override"],
        },
    )
    await db.flush()

    from app.workers.tasks.workspace_run import workspace_run_task

    workspace_run_task.delay(
        tenant_id=tenant_id,
        run_id=str(run.id),
        correlation_id=run.correlation_id,
        extra_params={"sandbox_id": sandbox_id},
    )

    return {"run_id": str(run.id), "status": run.status, "gates": gate_result["gates"], "row_count": 1}


async def execute_run_validate(params: dict[str, Any], context: dict[str, Any]) -> dict:
    """Trigger an SDF validate run against workspace files."""
    return await _execute_privileged_run(params, context, run_type="sdf_validate")


async def execute_run_unit_tests(params: dict[str, Any], context: dict[str, Any]) -> dict:
    """Trigger a Jest unit test run against workspace files."""
    return await _execute_privileged_run(params, context, run_type="jest_unit_test")


async def _execute_privileged_run(params: dict[str, Any], context: dict[str, Any], run_type: str) -> dict:
    """Shared privileged run logic for validate/tests MCP tools."""
    from app.core.dependencies import has_permission
    from app.services import audit_service, runner_service
    from app.services import workspace_service as ws_svc

    db = context["db"]
    tenant_id = context["tenant_id"]
    actor_id = context.get("actor_id")
    actor_uuid = uuid.UUID(actor_id) if actor_id else None
    if actor_uuid is None:
        return {"error": "Actor ID required for privileged run", "row_count": 0}

    allowed = await has_permission(db, actor_uuid, "workspace.manage")
    if not allowed:
        return {"error": "Permission denied: workspace.manage required", "row_count": 0}

    workspace_id = uuid.UUID(params["workspace_id"])
    changeset_raw = params.get("changeset_id")
    if not changeset_raw:
        return {"error": "changeset_id is required for privileged run", "row_count": 0}
    changeset_id = uuid.UUID(changeset_raw)

    cs = await ws_svc.get_changeset(db, changeset_id, uuid.UUID(tenant_id))
    if cs is None or cs.workspace_id != workspace_id:
        return {"error": "Changeset not found for workspace", "row_count": 0}
    if cs.status != "approved":
        return {"error": f"Changeset must be approved before running (current: {cs.status})", "row_count": 0}

    run = await runner_service.create_run(
        db,
        tenant_id=uuid.UUID(tenant_id),
        workspace_id=workspace_id,
        run_type=run_type,
        triggered_by=actor_uuid,
        changeset_id=changeset_id,
    )
    await audit_service.log_event(
        db=db,
        tenant_id=uuid.UUID(tenant_id),
        category="workspace",
        action="workspace.run.triggered",
        actor_id=actor_uuid,
        resource_type="workspace_run",
        resource_id=str(run.id),
        correlation_id=run.correlation_id,
        payload={"run_type": run_type, "changeset_id": str(changeset_id)},
    )
    await db.flush()

    from app.workers.tasks.workspace_run import workspace_run_task

    workspace_run_task.delay(
        tenant_id=tenant_id,
        run_id=str(run.id),
        correlation_id=run.correlation_id,
    )

    return {"run_id": str(run.id), "status": run.status, "row_count": 1}
