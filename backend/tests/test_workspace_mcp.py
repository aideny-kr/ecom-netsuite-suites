"""MCP tool tests for Dev Workspace."""

import io
import uuid
import zipfile

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.governance import governed_execute, check_rate_limit, _rate_limits
from app.mcp.registry import TOOL_REGISTRY
from app.services import workspace_service as ws_svc
from tests.conftest import create_test_tenant, create_test_user


@pytest_asyncio.fixture
async def tenant(db: AsyncSession):
    t = await create_test_tenant(db, name="MCP WS Test", plan="pro")
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{t.id}'"))
    return t


@pytest_asyncio.fixture
async def user(db, tenant):
    u, _ = await create_test_user(db, tenant, role_name="admin")
    return u


@pytest_asyncio.fixture
async def workspace_with_files(db, tenant, user):
    ws = await ws_svc.create_workspace(db, tenant.id, "MCP WS", user.id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("src/index.ts", "export const app = 'hello';")
        zf.writestr("src/utils.ts", "export function helper() { return 42; }")
    await ws_svc.import_workspace(db, ws.id, tenant.id, buf.getvalue())
    return ws


# --- Tool Registration ---

def test_workspace_tools_registered():
    assert "workspace.list_files" in TOOL_REGISTRY
    assert "workspace.read_file" in TOOL_REGISTRY
    assert "workspace.search" in TOOL_REGISTRY
    assert "workspace.propose_patch" in TOOL_REGISTRY


def test_apply_patch_not_in_registry():
    """apply_patch should NOT be an MCP tool — it's REST-only."""
    assert "workspace.apply_patch" not in TOOL_REGISTRY
    assert "workspace.apply" not in TOOL_REGISTRY


# --- Tool Execution via Governance ---

@pytest.mark.asyncio
async def test_list_files_tool(db, tenant, user, workspace_with_files):
    tool = TOOL_REGISTRY["workspace.list_files"]
    result = await governed_execute(
        tool_name="workspace.list_files",
        params={"workspace_id": str(workspace_with_files.id)},
        tenant_id=str(tenant.id),
        actor_id=str(user.id),
        execute_fn=tool["execute"],
        db=db,
    )
    assert "files" in result
    assert result["row_count"] >= 1


@pytest.mark.asyncio
async def test_read_file_tool(db, tenant, user, workspace_with_files):
    # First list files to get an ID
    tree = await ws_svc.list_files(db, workspace_with_files.id, tenant.id)
    file_node = _find_file(tree, "index.ts")
    assert file_node is not None

    tool = TOOL_REGISTRY["workspace.read_file"]
    result = await governed_execute(
        tool_name="workspace.read_file",
        params={
            "workspace_id": str(workspace_with_files.id),
            "file_id": file_node["id"],
        },
        tenant_id=str(tenant.id),
        actor_id=str(user.id),
        execute_fn=tool["execute"],
        db=db,
    )
    assert "content" in result
    assert "hello" in result["content"]


@pytest.mark.asyncio
async def test_search_tool(db, tenant, user, workspace_with_files):
    tool = TOOL_REGISTRY["workspace.search"]
    result = await governed_execute(
        tool_name="workspace.search",
        params={
            "workspace_id": str(workspace_with_files.id),
            "query": "helper",
            "search_type": "content",
        },
        tenant_id=str(tenant.id),
        actor_id=str(user.id),
        execute_fn=tool["execute"],
        db=db,
    )
    assert "results" in result
    assert result["row_count"] >= 1


@pytest.mark.asyncio
async def test_propose_patch_tool(db, tenant, user, workspace_with_files):
    tool = TOOL_REGISTRY["workspace.propose_patch"]
    result = await governed_execute(
        tool_name="workspace.propose_patch",
        params={
            "workspace_id": str(workspace_with_files.id),
            "file_path": "src/index.ts",
            "unified_diff": "--- a/src/index.ts\n+++ b/src/index.ts\n@@ -1 +1 @@\n-export const app = 'hello';\n+export const app = 'world';\n",
            "title": "Update app value",
            "rationale": "Testing patch proposal",
        },
        tenant_id=str(tenant.id),
        actor_id=str(user.id),
        execute_fn=tool["execute"],
        db=db,
    )
    assert "changeset_id" in result
    assert result["operation"] == "modify"


@pytest.mark.asyncio
async def test_propose_patch_creates_draft_not_applied(db, tenant, user, workspace_with_files):
    """propose_patch must create a draft changeset, NOT auto-apply."""
    tool = TOOL_REGISTRY["workspace.propose_patch"]
    result = await governed_execute(
        tool_name="workspace.propose_patch",
        params={
            "workspace_id": str(workspace_with_files.id),
            "file_path": "src/index.ts",
            "unified_diff": "--- a/src/index.ts\n+++ b/src/index.ts\n@@ -1 +1 @@\n-export const app = 'hello';\n+export const app = 'changed';\n",
            "title": "Draft test",
        },
        tenant_id=str(tenant.id),
        actor_id=str(user.id),
        execute_fn=tool["execute"],
        db=db,
    )
    # Verify the changeset is still draft
    cs = await ws_svc.get_changeset(db, uuid.UUID(result["changeset_id"]), tenant.id)
    assert cs.status == "draft"

    # Verify the file content is unchanged
    tree = await ws_svc.list_files(db, workspace_with_files.id, tenant.id)
    file_node = _find_file(tree, "index.ts")
    file_data = await ws_svc.read_file(db, workspace_with_files.id, uuid.UUID(file_node["id"]), tenant.id)
    assert "'hello'" in file_data["content"]


# --- Rate Limiting ---

def test_rate_limit_enforcement():
    """Ensure rate limits are enforced for workspace tools."""
    test_tenant = str(uuid.uuid4())
    # Clear any existing rate limits
    _rate_limits.pop(test_tenant, None)

    # workspace.propose_patch has rate_limit_per_minute=10
    for i in range(10):
        assert check_rate_limit(test_tenant, "workspace.propose_patch") is True

    # 11th call should be denied
    assert check_rate_limit(test_tenant, "workspace.propose_patch") is False

    # Clean up
    _rate_limits.pop(test_tenant, None)


# --- Helpers ---

def _find_file(tree: list[dict], name: str) -> dict | None:
    for node in tree:
        if node["name"] == name and not node["is_directory"]:
            return node
        if node.get("children"):
            found = _find_file(node["children"], name)
            if found:
                return found
    return None
