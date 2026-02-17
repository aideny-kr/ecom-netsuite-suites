"""Security tests for Dev Workspace: path traversal, size limits, injection."""

import io
import zipfile

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import workspace_service as ws_svc
from tests.conftest import create_test_tenant, create_test_user


@pytest_asyncio.fixture
async def tenant(db: AsyncSession):
    t = await create_test_tenant(db, name="Security Test", plan="pro")
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{t.id}'"))
    return t


@pytest_asyncio.fixture
async def user(db, tenant):
    u, _ = await create_test_user(db, tenant, role_name="admin")
    return u


@pytest_asyncio.fixture
async def workspace(db, tenant, user):
    return await ws_svc.create_workspace(db, tenant.id, "Security WS", user.id)


# --- Path Traversal ---


@pytest.mark.asyncio
async def test_path_traversal_dot_dot(db, tenant, user, workspace):
    """Path traversal via .. should be blocked."""
    with pytest.raises(ValueError, match="traversal"):
        await ws_svc.propose_patch(
            db,
            workspace.id,
            tenant.id,
            "../../../etc/passwd",
            "diff content",
            "Hack attempt",
            user.id,
        )


@pytest.mark.asyncio
async def test_path_traversal_encoded(db, tenant, user, workspace):
    """Encoded path traversal should be blocked by safe char check."""
    with pytest.raises(ValueError, match="disallowed"):
        await ws_svc.propose_patch(
            db,
            workspace.id,
            tenant.id,
            "src%2F..%2F..%2Fetc%2Fpasswd",
            "diff",
            "Encoded hack",
            user.id,
        )


@pytest.mark.asyncio
async def test_absolute_path_blocked_in_import(db, tenant, user, workspace):
    """Files with absolute paths in zip should be skipped."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("/etc/passwd", "root:x:0:0")
        zf.writestr("safe.txt", "ok")
    result = await ws_svc.import_workspace(db, workspace.id, tenant.id, buf.getvalue())
    assert result["skipped"] >= 1


# --- Size Limits ---


@pytest.mark.asyncio
async def test_oversized_file_rejected_in_import(db, tenant, user, workspace):
    """Files over 256KB should be skipped during import."""
    big = "x" * (ws_svc.MAX_FILE_SIZE + 1)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("huge.txt", big)
    result = await ws_svc.import_workspace(db, workspace.id, tenant.id, buf.getvalue())
    assert result["skipped"] >= 1
    assert result["imported"] == 0


@pytest.mark.asyncio
async def test_oversized_diff_rejected(db, tenant, user, workspace):
    """Diffs over 256KB should be rejected."""
    big_diff = "x" * (ws_svc.MAX_DIFF_SIZE + 1)
    with pytest.raises(ValueError, match="maximum size"):
        await ws_svc.propose_patch(
            db,
            workspace.id,
            tenant.id,
            "src/app.ts",
            big_diff,
            "Big diff",
            user.id,
        )


# --- Content Injection ---


@pytest.mark.asyncio
async def test_script_injection_stored_safely(db, tenant, user, workspace):
    """Script tags in content should be stored as-is (no XSS in storage)."""
    malicious = '<script>alert("xss")</script>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xss.html", malicious)
    await ws_svc.import_workspace(db, workspace.id, tenant.id, buf.getvalue())

    tree = await ws_svc.list_files(db, workspace.id, tenant.id)
    file_node = _find_file(tree, "xss.html")
    assert file_node is not None

    import uuid as _uuid

    result = await ws_svc.read_file(db, workspace.id, _uuid.UUID(file_node["id"]), tenant.id)
    # Content should be stored verbatim — no execution, no sanitization
    assert "<script>" in result["content"]


@pytest.mark.asyncio
async def test_null_byte_path_blocked(db, tenant, user, workspace):
    """Null bytes in paths should be rejected."""
    with pytest.raises(ValueError, match="disallowed"):
        ws_svc.validate_path("src/\x00evil.ts")


@pytest.mark.asyncio
async def test_newline_path_blocked(db, tenant, user, workspace):
    """Newlines in paths should be rejected."""
    with pytest.raises(ValueError, match="disallowed"):
        ws_svc.validate_path("src/\nevil.ts")


@pytest.mark.asyncio
async def test_binary_file_skipped_in_import(db, tenant, user, workspace):
    """Binary files (non-UTF8) should be skipped."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("binary.bin", bytes(range(256)))
        zf.writestr("text.txt", "valid")
    result = await ws_svc.import_workspace(db, workspace.id, tenant.id, buf.getvalue())
    # binary.bin should be skipped
    assert result["skipped"] >= 1


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
