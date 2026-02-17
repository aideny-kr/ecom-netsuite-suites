"""Unit tests for Dev Workspace: CRUD, state machine, patch, conflict detection, path validation, size limits."""

import hashlib
import io
import uuid
import zipfile

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace import WorkspacePatch
from app.services import workspace_service as ws_svc
from tests.conftest import create_test_tenant, create_test_user

# --- Fixtures ---


@pytest_asyncio.fixture
async def tenant(db: AsyncSession):
    t = await create_test_tenant(db, name="WS Test Corp", plan="pro")
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{t.id}'"))
    return t


@pytest_asyncio.fixture
async def user(db: AsyncSession, tenant):
    u, _ = await create_test_user(db, tenant, role_name="admin")
    return u


@pytest_asyncio.fixture
async def workspace(db: AsyncSession, tenant, user):
    ws = await ws_svc.create_workspace(db, tenant.id, "Test Workspace", user.id, "A test workspace")
    return ws


def _make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buf.getvalue()


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# --- Workspace CRUD ---


@pytest.mark.asyncio
async def test_create_workspace(db, tenant, user):
    ws = await ws_svc.create_workspace(db, tenant.id, "My Workspace", user.id)
    assert ws.name == "My Workspace"
    assert ws.status == "active"
    assert ws.tenant_id == tenant.id


@pytest.mark.asyncio
async def test_list_workspaces(db, tenant, user):
    await ws_svc.create_workspace(db, tenant.id, "WS1", user.id)
    await ws_svc.create_workspace(db, tenant.id, "WS2", user.id)
    result = await ws_svc.list_workspaces(db, tenant.id)
    assert len(result) >= 2


@pytest.mark.asyncio
async def test_archive_workspace(db, tenant, user, workspace):
    result = await ws_svc.archive_workspace(db, workspace.id, tenant.id)
    assert result.status == "archived"
    # Archived workspace should not appear in list
    active = await ws_svc.list_workspaces(db, tenant.id)
    ids = [ws.id for ws in active]
    assert workspace.id not in ids


@pytest.mark.asyncio
async def test_get_workspace_not_found(db, tenant):
    result = await ws_svc.get_workspace(db, uuid.uuid4(), tenant.id)
    assert result is None


# --- Import ---


@pytest.mark.asyncio
async def test_import_workspace(db, tenant, user, workspace):
    zip_bytes = _make_zip(
        {
            "src/main.ts": "console.log('hello');",
            "src/utils/helper.ts": "export function help() {}",
            "README.md": "# My Project",
        }
    )
    result = await ws_svc.import_workspace(db, workspace.id, tenant.id, zip_bytes)
    assert result["imported"] >= 3  # files + auto-created dirs


@pytest.mark.asyncio
async def test_import_invalid_zip(db, tenant, user, workspace):
    with pytest.raises(ValueError, match="Invalid zip"):
        await ws_svc.import_workspace(db, workspace.id, tenant.id, b"not a zip")


@pytest.mark.asyncio
async def test_import_skips_oversized_files(db, tenant, user, workspace):
    big_content = "x" * (ws_svc.MAX_FILE_SIZE + 1)
    zip_bytes = _make_zip({"big.txt": big_content, "small.txt": "ok"})
    result = await ws_svc.import_workspace(db, workspace.id, tenant.id, zip_bytes)
    assert result["skipped"] >= 1


# --- File Operations ---


@pytest.mark.asyncio
async def test_list_files_tree(db, tenant, user, workspace):
    zip_bytes = _make_zip(
        {
            "src/index.ts": "export {};",
            "src/lib/utils.ts": "export {};",
        }
    )
    await ws_svc.import_workspace(db, workspace.id, tenant.id, zip_bytes)
    tree = await ws_svc.list_files(db, workspace.id, tenant.id)
    assert len(tree) > 0


@pytest.mark.asyncio
async def test_read_file(db, tenant, user, workspace):
    zip_bytes = _make_zip({"test.txt": "line1\nline2\nline3\n"})
    await ws_svc.import_workspace(db, workspace.id, tenant.id, zip_bytes)
    tree = await ws_svc.list_files(db, workspace.id, tenant.id)
    # Find the file node
    file_node = _find_file(tree, "test.txt")
    assert file_node is not None
    result = await ws_svc.read_file(db, workspace.id, uuid.UUID(file_node["id"]), tenant.id)
    assert "line1" in result["content"]
    assert result["total_lines"] == 3


@pytest.mark.asyncio
async def test_read_file_with_line_range(db, tenant, user, workspace):
    content = "\n".join(f"line{i}" for i in range(1, 101))
    zip_bytes = _make_zip({"big.txt": content})
    await ws_svc.import_workspace(db, workspace.id, tenant.id, zip_bytes)
    tree = await ws_svc.list_files(db, workspace.id, tenant.id)
    file_node = _find_file(tree, "big.txt")
    result = await ws_svc.read_file(db, workspace.id, uuid.UUID(file_node["id"]), tenant.id, line_start=10, line_end=20)
    assert result["truncated"] is True
    assert "line10" in result["content"]


@pytest.mark.asyncio
async def test_read_file_not_found(db, tenant, user, workspace):
    result = await ws_svc.read_file(db, workspace.id, uuid.uuid4(), tenant.id)
    assert result is None


@pytest.mark.asyncio
async def test_search_files_by_name(db, tenant, user, workspace):
    zip_bytes = _make_zip({"src/index.ts": "hello", "src/utils.ts": "world"})
    await ws_svc.import_workspace(db, workspace.id, tenant.id, zip_bytes)
    results = await ws_svc.search_files(db, workspace.id, tenant.id, "utils")
    assert len(results) >= 1
    assert any("utils" in r["path"] for r in results)


@pytest.mark.asyncio
async def test_search_files_by_content(db, tenant, user, workspace):
    zip_bytes = _make_zip({"code.ts": "function fooBar() { return 42; }"})
    await ws_svc.import_workspace(db, workspace.id, tenant.id, zip_bytes)
    results = await ws_svc.search_files(db, workspace.id, tenant.id, "fooBar", search_type="content")
    assert len(results) >= 1


# --- Path Validation ---


@pytest.mark.asyncio
async def test_path_traversal_blocked():
    with pytest.raises(ValueError, match="traversal"):
        ws_svc.validate_path("../../../etc/passwd")


@pytest.mark.asyncio
async def test_absolute_path_blocked():
    with pytest.raises(ValueError, match="Absolute"):
        ws_svc.validate_path("/etc/passwd")


@pytest.mark.asyncio
async def test_oversized_path_blocked():
    with pytest.raises(ValueError, match="1-512"):
        ws_svc.validate_path("a/" * 300)


@pytest.mark.asyncio
async def test_deep_path_blocked():
    with pytest.raises(ValueError, match="depth"):
        ws_svc.validate_path("/".join(["d"] * 25))


@pytest.mark.asyncio
async def test_special_chars_blocked():
    with pytest.raises(ValueError, match="disallowed"):
        ws_svc.validate_path("src/<script>.ts")


@pytest.mark.asyncio
async def test_valid_path_normalized():
    result = ws_svc.validate_path("src/components/Button.tsx")
    assert result == "src/components/Button.tsx"


# --- Changeset State Machine ---


@pytest.mark.asyncio
async def test_changeset_create(db, tenant, user, workspace):
    cs = await ws_svc.create_changeset(db, workspace.id, tenant.id, "Fix bug", user.id)
    assert cs.status == "draft"
    assert cs.title == "Fix bug"


@pytest.mark.asyncio
async def test_changeset_submit_approve_apply(db, tenant, user, workspace):
    cs = await ws_svc.create_changeset(db, workspace.id, tenant.id, "Feature", user.id)

    cs = await ws_svc.transition_changeset(db, cs.id, tenant.id, "submit", user.id)
    assert cs.status == "pending_review"

    cs = await ws_svc.transition_changeset(db, cs.id, tenant.id, "approve", user.id)
    assert cs.status == "approved"
    assert cs.reviewed_by == user.id


@pytest.mark.asyncio
async def test_changeset_reject(db, tenant, user, workspace):
    cs = await ws_svc.create_changeset(db, workspace.id, tenant.id, "Bad change", user.id)
    cs = await ws_svc.transition_changeset(db, cs.id, tenant.id, "submit", user.id)
    cs = await ws_svc.transition_changeset(db, cs.id, tenant.id, "reject", user.id, "Code quality issue")
    assert cs.status == "rejected"
    assert cs.rejection_reason == "Code quality issue"


@pytest.mark.asyncio
async def test_invalid_transition_raises(db, tenant, user, workspace):
    cs = await ws_svc.create_changeset(db, workspace.id, tenant.id, "Test", user.id)
    with pytest.raises(ValueError, match="not valid"):
        await ws_svc.transition_changeset(db, cs.id, tenant.id, "approve", user.id)


@pytest.mark.asyncio
async def test_applied_changeset_is_terminal(db, tenant, user, workspace):
    # Create file first
    zip_bytes = _make_zip({"src/app.ts": "const x = 1;"})
    await ws_svc.import_workspace(db, workspace.id, tenant.id, zip_bytes)

    cs = await ws_svc.create_changeset(db, workspace.id, tenant.id, "Test", user.id)
    # Add a create patch to avoid needing a real diff
    patch = WorkspacePatch(
        tenant_id=tenant.id,
        changeset_id=cs.id,
        file_path="new_file.ts",
        operation="create",
        new_content="const y = 2;",
        baseline_sha256=_sha256(""),
        apply_order=0,
    )
    db.add(patch)
    await db.flush()

    cs = await ws_svc.transition_changeset(db, cs.id, tenant.id, "submit", user.id)
    cs = await ws_svc.transition_changeset(db, cs.id, tenant.id, "approve", user.id)
    cs = await ws_svc.apply_changeset(db, cs.id, tenant.id, user.id)
    assert cs.status == "applied"

    with pytest.raises(ValueError, match="not valid"):
        await ws_svc.transition_changeset(db, cs.id, tenant.id, "submit", user.id)


# --- Patch Operations ---


@pytest.mark.asyncio
async def test_propose_patch(db, tenant, user, workspace):
    zip_bytes = _make_zip({"src/app.ts": "const x = 1;\n"})
    await ws_svc.import_workspace(db, workspace.id, tenant.id, zip_bytes)

    result = await ws_svc.propose_patch(
        db,
        workspace.id,
        tenant.id,
        "src/app.ts",
        "--- a/src/app.ts\n+++ b/src/app.ts\n@@ -1 +1 @@\n-const x = 1;\n+const x = 2;\n",
        "Update x value",
        user.id,
    )
    assert result["changeset_id"]
    assert result["operation"] == "modify"


@pytest.mark.asyncio
async def test_propose_patch_creates_for_new_file(db, tenant, user, workspace):
    result = await ws_svc.propose_patch(
        db,
        workspace.id,
        tenant.id,
        "new_file.ts",
        "--- /dev/null\n+++ b/new_file.ts\n@@ -0,0 +1 @@\n+console.log('new');\n",
        "Add new file",
        user.id,
    )
    assert result["operation"] == "create"


@pytest.mark.asyncio
async def test_apply_changeset_with_create(db, tenant, user, workspace):
    cs = await ws_svc.create_changeset(db, workspace.id, tenant.id, "Create file", user.id)
    patch = WorkspacePatch(
        tenant_id=tenant.id,
        changeset_id=cs.id,
        file_path="new.txt",
        operation="create",
        new_content="hello world",
        baseline_sha256=_sha256(""),
        apply_order=0,
    )
    db.add(patch)
    await db.flush()

    cs = await ws_svc.transition_changeset(db, cs.id, tenant.id, "submit", user.id)
    cs = await ws_svc.transition_changeset(db, cs.id, tenant.id, "approve", user.id)
    cs = await ws_svc.apply_changeset(db, cs.id, tenant.id, user.id)
    assert cs.status == "applied"


@pytest.mark.asyncio
async def test_apply_changeset_with_delete(db, tenant, user, workspace):
    zip_bytes = _make_zip({"to_delete.txt": "remove me"})
    await ws_svc.import_workspace(db, workspace.id, tenant.id, zip_bytes)

    cs = await ws_svc.create_changeset(db, workspace.id, tenant.id, "Delete file", user.id)
    patch = WorkspacePatch(
        tenant_id=tenant.id,
        changeset_id=cs.id,
        file_path="to_delete.txt",
        operation="delete",
        baseline_sha256=_sha256("remove me"),
        apply_order=0,
    )
    db.add(patch)
    await db.flush()

    cs = await ws_svc.transition_changeset(db, cs.id, tenant.id, "submit", user.id)
    cs = await ws_svc.transition_changeset(db, cs.id, tenant.id, "approve", user.id)
    cs = await ws_svc.apply_changeset(db, cs.id, tenant.id, user.id)
    assert cs.status == "applied"


# --- Conflict Detection ---


@pytest.mark.asyncio
async def test_conflict_detection(db, tenant, user, workspace):
    zip_bytes = _make_zip({"src/app.ts": "const x = 1;\n"})
    await ws_svc.import_workspace(db, workspace.id, tenant.id, zip_bytes)

    cs = await ws_svc.create_changeset(db, workspace.id, tenant.id, "Change x", user.id)
    patch = WorkspacePatch(
        tenant_id=tenant.id,
        changeset_id=cs.id,
        file_path="src/app.ts",
        operation="modify",
        unified_diff="--- a/src/app.ts\n+++ b/src/app.ts\n@@ -1 +1 @@\n-const x = 1;\n+const x = 2;\n",
        baseline_sha256="wrong_hash_to_trigger_conflict",
        apply_order=0,
    )
    db.add(patch)
    await db.flush()

    cs = await ws_svc.transition_changeset(db, cs.id, tenant.id, "submit", user.id)
    cs = await ws_svc.transition_changeset(db, cs.id, tenant.id, "approve", user.id)

    with pytest.raises(ValueError, match="Conflict"):
        await ws_svc.apply_changeset(db, cs.id, tenant.id, user.id)


@pytest.mark.asyncio
async def test_apply_unapproved_changeset_blocked(db, tenant, user, workspace):
    cs = await ws_svc.create_changeset(db, workspace.id, tenant.id, "Draft", user.id)
    with pytest.raises(ValueError, match="approved"):
        await ws_svc.apply_changeset(db, cs.id, tenant.id, user.id)


# --- Changeset Diff ---


@pytest.mark.asyncio
async def test_get_changeset_diff(db, tenant, user, workspace):
    cs = await ws_svc.create_changeset(db, workspace.id, tenant.id, "Create file", user.id)
    patch = WorkspacePatch(
        tenant_id=tenant.id,
        changeset_id=cs.id,
        file_path="new.txt",
        operation="create",
        new_content="hello",
        baseline_sha256=_sha256(""),
        apply_order=0,
    )
    db.add(patch)
    await db.flush()

    diff = await ws_svc.get_changeset_diff(db, cs.id, tenant.id)
    assert diff is not None
    assert diff["title"] == "Create file"
    assert len(diff["files"]) == 1
    assert diff["files"][0]["operation"] == "create"


@pytest.mark.asyncio
async def test_get_changeset_diff_not_found(db, tenant):
    diff = await ws_svc.get_changeset_diff(db, uuid.uuid4(), tenant.id)
    assert diff is None


# --- Idempotency ---


@pytest.mark.asyncio
async def test_import_twice_same_files(db, tenant, user, workspace):
    """Importing the same zip twice should fail on unique constraint (workspace_id, path)."""
    zip_bytes = _make_zip({"src/app.ts": "const x = 1;"})
    await ws_svc.import_workspace(db, workspace.id, tenant.id, zip_bytes)
    # Second import should raise due to unique constraint
    with pytest.raises(Exception):
        await ws_svc.import_workspace(db, workspace.id, tenant.id, zip_bytes)


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
