"""Onboarding-specific tool definitions and execution for chat-based onboarding."""

from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Tool definitions in Anthropic format
ONBOARDING_TOOL_DEFINITIONS = [
    {
        "name": "save_onboarding_profile",
        "description": (
            "Save the user's onboarding profile after they confirm. "
            "Call this when the user has reviewed and approved the summary of their information."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "industry": {
                    "type": "string",
                    "description": "The user's industry (e.g., retail, wholesale, SaaS, manufacturing)",
                },
                "business_description": {
                    "type": "string",
                    "description": "Brief description of the user's business",
                },
                "netsuite_account_id": {
                    "type": "string",
                    "description": "NetSuite account ID if provided",
                },
                "chart_of_accounts": {
                    "type": "array",
                    "description": "Chart of accounts configuration",
                    "items": {"type": "object"},
                },
                "subsidiaries": {
                    "type": "array",
                    "description": "List of subsidiaries",
                    "items": {"type": "object"},
                },
                "item_types": {
                    "type": "array",
                    "description": "Item types in use",
                    "items": {"type": "string"},
                },
                "custom_segments": {
                    "type": "object",
                    "description": "Custom segment configuration",
                },
                "fiscal_calendar": {
                    "type": "object",
                    "description": "Fiscal calendar configuration",
                },
                "suiteql_naming": {
                    "type": "object",
                    "description": "SuiteQL naming conventions",
                },
            },
            "required": [],
        },
    },
    {
        "name": "start_netsuite_oauth",
        "description": "Generate a NetSuite OAuth 2.0 authorization URL for the user to connect their account.",
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "string",
                    "description": "The NetSuite account ID (e.g., 1234567 or TSTDRV1234567)",
                },
            },
            "required": ["account_id"],
        },
    },
    {
        "name": "check_netsuite_connection",
        "description": "Check if the tenant has an active NetSuite connection.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "workspace_list_workspaces",
        "description": "List active workspaces for the current tenant.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "workspace_list_files",
        "description": "List files in a workspace so we can pick a script to edit.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID"},
                "directory": {"type": "string", "description": "Optional directory prefix"},
                "recursive": {"type": "boolean", "description": "Whether to recurse subdirectories"},
            },
            "required": ["workspace_id"],
        },
    },
    {
        "name": "workspace_read_file",
        "description": "Read a specific file from a workspace to explain its behavior.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID"},
                "file_id": {"type": "string", "description": "Workspace file UUID"},
                "line_start": {"type": "integer"},
                "line_end": {"type": "integer"},
            },
            "required": ["workspace_id", "file_id"],
        },
    },
    {
        "name": "workspace_propose_patch",
        "description": "Create a draft changeset by proposing a unified diff for a workspace file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string"},
                "file_path": {"type": "string"},
                "unified_diff": {"type": "string"},
                "title": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["workspace_id", "file_path", "unified_diff", "title"],
        },
    },
    {
        "name": "workspace_submit_changeset",
        "description": "Submit a draft changeset for review.",
        "input_schema": {
            "type": "object",
            "properties": {
                "changeset_id": {"type": "string"},
            },
            "required": ["changeset_id"],
        },
    },
    {
        "name": "workspace_approve_changeset",
        "description": "Approve a pending changeset so validate/tests can run.",
        "input_schema": {
            "type": "object",
            "properties": {
                "changeset_id": {"type": "string"},
            },
            "required": ["changeset_id"],
        },
    },
    {
        "name": "workspace_run_validate",
        "description": "Run validate on an approved changeset.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string"},
                "changeset_id": {"type": "string"},
            },
            "required": ["workspace_id", "changeset_id"],
        },
    },
    {
        "name": "workspace_run_unit_tests",
        "description": "Run unit tests on an approved changeset.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string"},
                "changeset_id": {"type": "string"},
            },
            "required": ["workspace_id", "changeset_id"],
        },
    },
    {
        "name": "workspace_get_run_status",
        "description": "Get status and summary artifacts for a run.",
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
            },
            "required": ["run_id"],
        },
    },
]


async def execute_onboarding_tool(
    tool_name: str,
    tool_input: dict,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> str:
    """Execute an onboarding tool call and return the result as a JSON string."""
    if tool_name == "save_onboarding_profile":
        return await _save_onboarding_profile(tool_input, tenant_id, user_id, db)
    if tool_name == "start_netsuite_oauth":
        return await _start_netsuite_oauth(tool_input, tenant_id, db)
    if tool_name == "check_netsuite_connection":
        return await _check_netsuite_connection(tenant_id, db)
    if tool_name == "workspace_list_workspaces":
        return await _workspace_list_workspaces(tenant_id, db)
    if tool_name == "workspace_list_files":
        return await _workspace_mcp_call("workspace.list_files", tool_input, tenant_id, user_id, db)
    if tool_name == "workspace_read_file":
        return await _workspace_mcp_call("workspace.read_file", tool_input, tenant_id, user_id, db)
    if tool_name == "workspace_propose_patch":
        return await _workspace_mcp_call("workspace.propose_patch", tool_input, tenant_id, user_id, db)
    if tool_name == "workspace_submit_changeset":
        return await _workspace_transition_changeset(tool_input, tenant_id, user_id, db, action="submit")
    if tool_name == "workspace_approve_changeset":
        return await _workspace_transition_changeset(tool_input, tenant_id, user_id, db, action="approve")
    if tool_name == "workspace_run_validate":
        return await _workspace_mcp_call("workspace.run_validate", tool_input, tenant_id, user_id, db)
    if tool_name == "workspace_run_unit_tests":
        return await _workspace_mcp_call("workspace.run_unit_tests", tool_input, tenant_id, user_id, db)
    if tool_name == "workspace_get_run_status":
        return await _workspace_get_run_status(tool_input, tenant_id, db)

    return json.dumps({"error": f"Unknown onboarding tool: {tool_name}"})


async def _save_onboarding_profile(
    tool_input: dict,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> str:
    """Create and confirm a tenant profile from onboarding data."""
    from app.services import onboarding_service

    try:
        profile = await onboarding_service.create_profile(
            db=db,
            tenant_id=tenant_id,
            data=tool_input,
            user_id=user_id,
        )
        confirmed = await onboarding_service.confirm_profile(
            db=db,
            tenant_id=tenant_id,
            profile_id=profile.id,
            user_id=user_id,
        )
        return json.dumps(
            {
                "success": True,
                "profile_id": str(confirmed.id),
                "message": "Profile saved and confirmed! Onboarding is complete.",
            }
        )
    except Exception as exc:
        logger.exception("Failed to save onboarding profile")
        return json.dumps({"error": f"Failed to save profile: {exc}"})


async def _start_netsuite_oauth(
    tool_input: dict,
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> str:
    """Generate a NetSuite OAuth authorization URL."""
    from app.services.netsuite_oauth_service import build_authorize_url, generate_pkce_pair

    account_id = tool_input.get("account_id", "")
    if not account_id:
        return json.dumps({"error": "account_id is required"})

    code_verifier, code_challenge = generate_pkce_pair()
    state = f"onboarding_{tenant_id.hex}"
    authorize_url = build_authorize_url(account_id, state, code_challenge)

    return json.dumps(
        {
            "authorize_url": authorize_url,
            "message": (
                "Please open this URL in a new browser tab to authorize "
                "the NetSuite connection. Once you've completed the authorization, "
                "let me know and I'll verify the connection."
            ),
        }
    )


async def _check_netsuite_connection(
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> str:
    """Check if the tenant has an active NetSuite connection."""
    from app.models.connection import Connection

    result = await db.execute(
        select(Connection).where(
            Connection.tenant_id == tenant_id,
            Connection.provider == "netsuite",
            Connection.status == "active",
        )
    )
    connection = result.scalar_one_or_none()

    if connection:
        return json.dumps(
            {
                "connected": True,
                "connection_id": str(connection.id),
                "message": "NetSuite is connected and active!",
            }
        )
    else:
        return json.dumps(
            {
                "connected": False,
                "message": "No active NetSuite connection found. The user may need to complete the OAuth flow.",
            }
        )


async def _workspace_list_workspaces(tenant_id: uuid.UUID, db: AsyncSession) -> str:
    from app.services import workspace_service as ws_svc

    workspaces = await ws_svc.list_workspaces(db, tenant_id)
    return json.dumps(
        {
            "workspaces": [{"id": str(ws.id), "name": ws.name, "description": ws.description} for ws in workspaces],
            "row_count": len(workspaces),
        }
    )


async def _workspace_mcp_call(
    tool_name: str,
    tool_input: dict,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> str:
    from app.mcp.server import mcp_server

    result = await mcp_server.call_tool(
        tool_name=tool_name,
        params=tool_input,
        tenant_id=str(tenant_id),
        actor_id=str(user_id),
        correlation_id=str(uuid.uuid4()),
        db=db,
    )
    return json.dumps(result, default=str)


async def _workspace_transition_changeset(
    tool_input: dict,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
    action: str,
) -> str:
    from app.core.dependencies import has_permission
    from app.services import workspace_service as ws_svc

    changeset_raw = tool_input.get("changeset_id")
    if not changeset_raw:
        return json.dumps({"error": "changeset_id is required"})

    changeset_id = uuid.UUID(changeset_raw)
    if action == "approve":
        can_review = await has_permission(db, user_id, "workspace.review")
        if not can_review:
            return json.dumps({"error": "Permission denied: workspace.review required"})

    try:
        cs = await ws_svc.transition_changeset(db, changeset_id, tenant_id, action, user_id)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    return json.dumps(
        {
            "changeset_id": str(cs.id),
            "status": cs.status,
            "action": action,
        }
    )


async def _workspace_get_run_status(tool_input: dict, tenant_id: uuid.UUID, db: AsyncSession) -> str:
    from sqlalchemy import select

    from app.models.workspace import WorkspaceArtifact
    from app.services import runner_service

    run_id_raw = tool_input.get("run_id")
    if not run_id_raw:
        return json.dumps({"error": "run_id is required"})

    run = await runner_service.get_run(db, uuid.UUID(run_id_raw), tenant_id)
    if run is None:
        return json.dumps({"error": "Run not found"})

    artifact_result = await db.execute(
        select(WorkspaceArtifact)
        .where(
            WorkspaceArtifact.run_id == run.id,
            WorkspaceArtifact.tenant_id == tenant_id,
        )
        .order_by(WorkspaceArtifact.created_at.asc())
    )
    artifacts = artifact_result.scalars().all()
    return json.dumps(
        {
            "run_id": str(run.id),
            "status": run.status,
            "run_type": run.run_type,
            "artifacts": [
                {
                    "id": str(a.id),
                    "artifact_type": a.artifact_type,
                    "size_bytes": a.size_bytes,
                }
                for a in artifacts
            ],
        }
    )
