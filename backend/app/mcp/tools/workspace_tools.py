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

    # Enqueue auto-validate via the orchestrator. The patch is already applied —
    # if the queue itself errors (e.g. dev container with no `_create_run`
    # closure wired by the lifespan), log it but don't fail the apply.
    try:
        from app.services.workspace.auto_validate_orchestrator import get_orchestrator

        await get_orchestrator().enqueue(
            workspace_id=cs.workspace_id,
            changeset_id=cs.id,
            tenant_id=uuid.UUID(tenant_id),
            triggered_by=actor_uuid,
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "workspace.apply_patch.enqueue_failed",
            changeset_id=str(cs.id),
            error=str(exc),
        )

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
    """Build a deploy preview + mint an HMAC token. Does NOT queue the run.

    Closes codex P1 #2 — the legacy MCP path queued deploys directly,
    bypassing the workspace UI's two-step gate. Now the MCP tool returns
    the same preview payload as the HTTP /preview endpoint, plus a
    ``confirmation_required`` marker that the orchestrator's
    ``_intercept_tool_result`` recognizes and surfaces as an SSE
    ``confirmation_required`` event to the user. The agent waits for the
    user to confirm via the workspace UI (or a chat confirmation card),
    which calls ``workspace.deploy_sandbox_confirm`` to actually queue
    the run.
    """
    from app.core.dependencies import has_permission
    from app.services.deploy_preview_service import (
        DeployPreviewError,
        build_deploy_preview,
        mint_deploy_token,
    )

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

    require_assertions = params.get("require_assertions", False)

    try:
        preview = await build_deploy_preview(
            db=db,
            changeset_id=changeset_id,
            sandbox_id=sandbox_id,
            require_assertions=require_assertions,
            tenant_id=uuid.UUID(tenant_id),
            actor_id=actor_uuid,
        )
        minted = await mint_deploy_token(db=db, preview=preview)
    except DeployPreviewError as e:
        return {"error": str(e), "row_count": 0}

    await db.flush()

    return {
        "confirmation_required": True,
        "confirmation_type": "sandbox_deploy",
        "preview": {**preview, **minted},
        "row_count": 0,
    }


async def execute_deploy_sandbox_confirm(params: dict[str, Any], context: dict[str, Any]) -> dict:
    """Verify a deploy-preview token and queue the workspace_run.

    Caller MUST have first received a ``confirmation_required`` payload
    from ``execute_deploy_sandbox`` (or the HTTP /preview endpoint). The
    user reviews + clicks Confirm in the workspace UI (or chat card),
    which calls back into this tool with the jti + confirmation_token.
    """
    from app.core.dependencies import has_permission
    from app.services import runner_service
    from app.services.deploy_preview_service import (
        DeployPreviewError,
        verify_and_consume_deploy_token,
    )

    db = context["db"]
    tenant_id = context["tenant_id"]
    actor_id = context.get("actor_id")
    actor_uuid = uuid.UUID(actor_id) if actor_id else None
    if actor_uuid is None:
        return {"error": "Actor ID required", "row_count": 0}

    allowed = await has_permission(db, actor_uuid, "workspace.manage")
    if not allowed:
        return {"error": "Permission denied: workspace.manage required", "row_count": 0}

    jti_raw = params.get("jti")
    token = params.get("confirmation_token")
    if not jti_raw or not token:
        return {"error": "jti and confirmation_token are required", "row_count": 0}

    try:
        result = await verify_and_consume_deploy_token(
            db=db,
            jti=uuid.UUID(jti_raw),
            confirmation_token=token,
            actor_id=actor_uuid,
            tenant_id=uuid.UUID(tenant_id),
        )
    except DeployPreviewError as e:
        return {"error": str(e), "row_count": 0}

    token_row = result["row"]
    changeset = result["changeset"]

    run = await runner_service.create_run(
        db,
        tenant_id=uuid.UUID(tenant_id),
        workspace_id=changeset.workspace_id,
        run_type="deploy_sandbox",
        triggered_by=actor_uuid,
        changeset_id=changeset.id,
    )
    token_row.consumed_run_id = run.id
    await db.flush()

    from app.workers.tasks.workspace_run import workspace_run_task

    workspace_run_task.delay(
        tenant_id=tenant_id,
        run_id=str(run.id),
        correlation_id=run.correlation_id,
        extra_params={
            "sandbox_id": token_row.sandbox_id,
            "expected_snapshot_sha": token_row.snapshot_sha,
        },
    )

    return {
        "run_id": str(run.id),
        "status": run.status,
        "row_count": 1,
    }


async def execute_run_validate(params: dict[str, Any], context: dict[str, Any]) -> dict:
    """Trigger a SuiteCloud validate run against workspace files."""
    return await _execute_privileged_run(params, context, run_type="suitecloud_validate")


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
