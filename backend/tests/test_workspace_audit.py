"""HTTP-level tests for Dev Workspace audit event emission."""

import io
import zipfile

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent
from app.services import workspace_service as ws_svc
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers


@pytest_asyncio.fixture
async def tenant(db: AsyncSession):
    t = await create_test_tenant(db, name="Audit WS Test", plan="pro")
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{t.id}'"))
    return t


@pytest_asyncio.fixture
async def admin(db, tenant):
    user, _ = await create_test_user(db, tenant, role_name="admin")
    return user, make_auth_headers(user)


@pytest_asyncio.fixture
async def workspace_with_files(db, tenant, admin):
    user, _ = admin
    ws = await ws_svc.create_workspace(db, tenant.id, "Audit WS", user.id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("src/main.ts", "console.log('audit');")
        zf.writestr("src/lib.ts", "export const x = 1;")
    await ws_svc.import_workspace(db, ws.id, tenant.id, buf.getvalue())
    return ws


def _find_file(tree: list[dict], name: str) -> dict | None:
    for node in tree:
        if node["name"] == name and not node["is_directory"]:
            return node
        if node.get("children"):
            found = _find_file(node["children"], name)
            if found:
                return found
    return None


@pytest.mark.asyncio
async def test_list_files_emits_audit(client, db, tenant, admin, workspace_with_files):
    """GET /workspaces/{id}/files emits workspace.files.listed audit event."""
    _, headers = admin
    resp = await client.get(f"/api/v1/workspaces/{workspace_with_files.id}/files", headers=headers)
    assert resp.status_code == 200

    result = await db.execute(
        select(AuditEvent).where(
            AuditEvent.action == "workspace.files.listed",
            AuditEvent.resource_id == str(workspace_with_files.id),
            AuditEvent.tenant_id == tenant.id,
        )
    )
    event = result.scalars().first()
    assert event is not None
    assert event.category == "workspace"
    assert "file_count" in event.payload


@pytest.mark.asyncio
async def test_read_file_emits_audit(client, db, tenant, admin, workspace_with_files):
    """GET /workspaces/{id}/files/{file_id} emits workspace.file.read audit event."""
    user, headers = admin
    # Get file ID
    tree = await ws_svc.list_files(db, workspace_with_files.id, tenant.id)
    file_node = _find_file(tree, "main.ts")
    assert file_node is not None

    resp = await client.get(
        f"/api/v1/workspaces/{workspace_with_files.id}/files/{file_node['id']}", headers=headers
    )
    assert resp.status_code == 200

    result = await db.execute(
        select(AuditEvent).where(
            AuditEvent.action == "workspace.file.read",
            AuditEvent.resource_id == str(workspace_with_files.id),
            AuditEvent.tenant_id == tenant.id,
        )
    )
    event = result.scalars().first()
    assert event is not None
    assert event.category == "workspace"
    assert event.payload["file_id"] == file_node["id"]


@pytest.mark.asyncio
async def test_search_files_emits_audit(client, db, tenant, admin, workspace_with_files):
    """GET /workspaces/{id}/search emits workspace.files.searched audit event."""
    _, headers = admin
    resp = await client.get(
        f"/api/v1/workspaces/{workspace_with_files.id}/search",
        params={"query": "audit", "search_type": "content"},
        headers=headers,
    )
    assert resp.status_code == 200

    result = await db.execute(
        select(AuditEvent).where(
            AuditEvent.action == "workspace.files.searched",
            AuditEvent.resource_id == str(workspace_with_files.id),
            AuditEvent.tenant_id == tenant.id,
        )
    )
    event = result.scalars().first()
    assert event is not None
    assert event.category == "workspace"
    assert event.payload["query"] == "audit"
    assert "result_count" in event.payload


@pytest.mark.asyncio
async def test_changeset_transition_emits_transitioned(client, db, tenant, admin, workspace_with_files):
    """POST /changesets/{id}/transition emits changeset.transitioned event with from/to status."""
    user, headers = admin
    cs = await ws_svc.create_changeset(db, workspace_with_files.id, tenant.id, "Trans CS", user.id)
    await db.flush()

    resp = await client.post(
        f"/api/v1/changesets/{cs.id}/transition",
        json={"action": "submit"},
        headers=headers,
    )
    assert resp.status_code == 200

    result = await db.execute(
        select(AuditEvent).where(
            AuditEvent.action == "changeset.transitioned",
            AuditEvent.resource_id == str(cs.id),
            AuditEvent.tenant_id == tenant.id,
        )
    )
    event = result.scalars().first()
    assert event is not None
    assert event.category == "workspace"
    assert event.payload["from_status"] == "draft"
    assert event.payload["to_status"] == "pending_review"
    assert event.payload["action"] == "submit"
