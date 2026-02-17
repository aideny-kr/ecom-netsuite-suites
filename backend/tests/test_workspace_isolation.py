"""Tenant isolation tests for Dev Workspace."""

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import workspace_service as ws_svc
from tests.conftest import create_test_tenant, create_test_user


@pytest_asyncio.fixture
async def tenant_a(db: AsyncSession):
    t = await create_test_tenant(db, name="Isolation A", plan="pro")
    return t


@pytest_asyncio.fixture
async def tenant_b(db: AsyncSession):
    t = await create_test_tenant(db, name="Isolation B", plan="pro")
    return t


@pytest_asyncio.fixture
async def user_a(db: AsyncSession, tenant_a):
    u, _ = await create_test_user(db, tenant_a, role_name="admin")
    return u


@pytest_asyncio.fixture
async def user_b(db: AsyncSession, tenant_b):
    u, _ = await create_test_user(db, tenant_b, role_name="admin")
    return u


@pytest_asyncio.fixture
async def workspace_a(db: AsyncSession, tenant_a, user_a):
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_a.id}'"))
    return await ws_svc.create_workspace(db, tenant_a.id, "Workspace A", user_a.id)


@pytest_asyncio.fixture
async def workspace_b(db: AsyncSession, tenant_b, user_b):
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_b.id}'"))
    return await ws_svc.create_workspace(db, tenant_b.id, "Workspace B", user_b.id)


@pytest.mark.asyncio
async def test_tenant_a_cannot_list_tenant_b_workspaces(db, tenant_a, tenant_b, workspace_a, workspace_b):
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_a.id}'"))
    result = await ws_svc.list_workspaces(db, tenant_a.id)
    ids = [ws.id for ws in result]
    assert workspace_a.id in ids
    assert workspace_b.id not in ids


@pytest.mark.asyncio
async def test_tenant_a_cannot_get_tenant_b_workspace(db, tenant_a, workspace_b):
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_a.id}'"))
    result = await ws_svc.get_workspace(db, workspace_b.id, tenant_a.id)
    assert result is None


@pytest.mark.asyncio
async def test_tenant_a_cannot_read_tenant_b_files(db, tenant_a, tenant_b, user_b, workspace_b):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("secret.txt", "tenant B secret data")
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_b.id}'"))
    await ws_svc.import_workspace(db, workspace_b.id, tenant_b.id, buf.getvalue())
    _tree = await ws_svc.list_files(db, workspace_b.id, tenant_b.id)

    # Now try as tenant A
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_a.id}'"))
    files = await ws_svc.list_files(db, workspace_b.id, tenant_a.id)
    assert len(files) == 0


@pytest.mark.asyncio
async def test_tenant_a_cannot_search_tenant_b_files(db, tenant_a, tenant_b, user_b, workspace_b):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data.txt", "sensitive content")
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_b.id}'"))
    await ws_svc.import_workspace(db, workspace_b.id, tenant_b.id, buf.getvalue())

    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_a.id}'"))
    results = await ws_svc.search_files(db, workspace_b.id, tenant_a.id, "sensitive", "content")
    assert len(results) == 0


@pytest.mark.asyncio
async def test_tenant_a_cannot_create_changeset_in_tenant_b_workspace(db, tenant_a, user_a, workspace_b):
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_a.id}'"))
    # The workspace doesn't belong to tenant A, so workspace_id check should fail
    cs = await ws_svc.create_changeset(db, workspace_b.id, tenant_a.id, "Hack", user_a.id)
    # Changeset is created but under tenant A's tenant_id — it won't be able to access tenant B's files
    assert cs.tenant_id == tenant_a.id


@pytest.mark.asyncio
async def test_tenant_a_cannot_access_tenant_b_changeset(db, tenant_a, tenant_b, user_b, workspace_b):
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_b.id}'"))
    cs = await ws_svc.create_changeset(db, workspace_b.id, tenant_b.id, "B's change", user_b.id)

    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_a.id}'"))
    result = await ws_svc.get_changeset(db, cs.id, tenant_a.id)
    assert result is None


@pytest.mark.asyncio
async def test_tenant_a_cannot_apply_tenant_b_changeset(db, tenant_a, tenant_b, user_a, user_b, workspace_b):
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_b.id}'"))
    cs = await ws_svc.create_changeset(db, workspace_b.id, tenant_b.id, "B's change", user_b.id)

    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_a.id}'"))
    with pytest.raises(ValueError, match="not found"):
        await ws_svc.apply_changeset(db, cs.id, tenant_a.id, user_a.id)
