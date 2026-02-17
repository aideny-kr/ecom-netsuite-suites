"""RBAC tests for Dev Workspace endpoints."""

import io
import uuid
import zipfile

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import workspace_service as ws_svc
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers


@pytest_asyncio.fixture
async def tenant(db: AsyncSession):
    t = await create_test_tenant(db, name="RBAC Test", plan="pro")
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{t.id}'"))
    return t


@pytest_asyncio.fixture
async def admin(db, tenant):
    user, _ = await create_test_user(db, tenant, role_name="admin")
    return user, make_auth_headers(user)


@pytest_asyncio.fixture
async def readonly(db, tenant):
    user, _ = await create_test_user(db, tenant, role_name="readonly")
    return user, make_auth_headers(user)


@pytest_asyncio.fixture
async def workspace(db, tenant, admin):
    user, _ = admin
    return await ws_svc.create_workspace(db, tenant.id, "RBAC WS", user.id)


# --- View permission tests ---

@pytest.mark.asyncio
async def test_readonly_can_list_workspaces(client, readonly, tenant, workspace):
    _, headers = readonly
    resp = await client.get("/api/v1/workspaces", headers=headers)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_readonly_can_get_workspace(client, readonly, workspace):
    _, headers = readonly
    resp = await client.get(f"/api/v1/workspaces/{workspace.id}", headers=headers)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_readonly_can_list_files(client, readonly, workspace):
    _, headers = readonly
    resp = await client.get(f"/api/v1/workspaces/{workspace.id}/files", headers=headers)
    assert resp.status_code == 200


# --- Manage permission tests (readonly should be denied) ---

@pytest.mark.asyncio
async def test_readonly_cannot_create_workspace(client, readonly):
    _, headers = readonly
    resp = await client.post(
        "/api/v1/workspaces",
        json={"name": "Forbidden WS"},
        headers=headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_readonly_cannot_delete_workspace(client, readonly, workspace):
    _, headers = readonly
    resp = await client.delete(f"/api/v1/workspaces/{workspace.id}", headers=headers)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_readonly_cannot_import(client, readonly, workspace):
    _, headers = readonly
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("test.txt", "hello")
    buf.seek(0)
    resp = await client.post(
        f"/api/v1/workspaces/{workspace.id}/import",
        files={"file": ("test.zip", buf.getvalue(), "application/zip")},
        headers=headers,
    )
    assert resp.status_code == 403


# --- Review permission tests ---

@pytest.mark.asyncio
async def test_readonly_cannot_transition_changeset(client, db, tenant, readonly, admin, workspace):
    admin_user, _ = admin
    cs = await ws_svc.create_changeset(db, workspace.id, tenant.id, "Test CS", admin_user.id)
    await ws_svc.transition_changeset(db, cs.id, tenant.id, "submit", admin_user.id)
    await db.flush()

    _, headers = readonly
    resp = await client.post(
        f"/api/v1/changesets/{cs.id}/transition",
        json={"action": "approve"},
        headers=headers,
    )
    assert resp.status_code == 403


# --- Apply permission tests ---

@pytest.mark.asyncio
async def test_readonly_cannot_apply_changeset(client, db, tenant, readonly, admin, workspace):
    admin_user, _ = admin
    cs = await ws_svc.create_changeset(db, workspace.id, tenant.id, "Test CS", admin_user.id)
    await ws_svc.transition_changeset(db, cs.id, tenant.id, "submit", admin_user.id)
    await ws_svc.transition_changeset(db, cs.id, tenant.id, "approve", admin_user.id)
    await db.flush()

    _, headers = readonly
    resp = await client.post(
        f"/api/v1/changesets/{cs.id}/apply",
        headers=headers,
    )
    assert resp.status_code == 403


# --- Admin can do everything ---

@pytest.mark.asyncio
async def test_admin_can_create_workspace(client, admin):
    _, headers = admin
    resp = await client.post(
        "/api/v1/workspaces",
        json={"name": "Admin WS"},
        headers=headers,
    )
    assert resp.status_code == 201
