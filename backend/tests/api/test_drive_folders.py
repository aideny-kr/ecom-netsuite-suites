from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.core.encryption import encrypt_credentials
from app.models.drive import DriveFolder
from app.models.feature_flag import TenantFeatureFlag
from app.models.mcp_connector import McpConnector
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers


async def _enable_drive_rag(db, tenant_id):
    existing = (
        (
            await db.execute(
                select(TenantFeatureFlag).where(
                    TenantFeatureFlag.tenant_id == tenant_id,
                    TenantFeatureFlag.flag_key == "drive_rag",
                )
            )
        )
        .scalars()
        .first()
    )
    if existing:
        existing.enabled = True
    else:
        db.add(TenantFeatureFlag(tenant_id=tenant_id, flag_key="drive_rag", enabled=True))
    await db.commit()
    # Clear any cached decision from a previous call
    from app.services.feature_flag_service import clear_cache

    clear_cache()


async def _add_sheets_connector(db, tenant_id):
    encrypted = encrypt_credentials({"service_account_json": {"client_email": "sa@test.iam.gserviceaccount.com"}})
    c = McpConnector(
        tenant_id=tenant_id,
        provider="google_sheets",
        label="test",
        server_url="https://sheets.googleapis.com",
        auth_type="service_account",
        encrypted_credentials=encrypted,
        encryption_key_version=1,
        status="active",
        is_enabled=True,
        metadata_json={"client_email": "sa@test.iam.gserviceaccount.com"},
    )
    db.add(c)
    await db.commit()
    return c


@pytest.mark.asyncio
async def test_list_drive_folders_empty(client, db):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    await _enable_drive_rag(db, tenant.id)
    resp = await client.get("/api/v1/drive-folders", headers=make_auth_headers(user))
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_drive_folders_returns_string_ids(client, db):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    await _enable_drive_rag(db, tenant.id)
    folder = DriveFolder(tenant_id=tenant.id, folder_id="X", folder_name="Policies", created_by=user.id)
    db.add(folder)
    await db.commit()
    resp = await client.get("/api/v1/drive-folders", headers=make_auth_headers(user))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert isinstance(body[0]["id"], str)
    assert isinstance(body[0]["tenant_id"], str)


@pytest.mark.asyncio
async def test_create_drive_folder_returns_string_ids(client, db):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    await _enable_drive_rag(db, tenant.id)
    await _add_sheets_connector(db, tenant.id)

    with (
        patch(
            "app.api.v1.drive_folders.drive_client.get_folder_metadata",
            new=AsyncMock(return_value={"id": "FOLDER", "name": "Policies", "mimeType": "x"}),
        ),
        patch("app.api.v1.drive_folders.drive_rag_sync_folder.delay"),
    ):
        resp = await client.post(
            "/api/v1/drive-folders",
            json={"folder_id_or_url": "https://drive.google.com/drive/folders/FOLDER"},
            headers=make_auth_headers(user),
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert isinstance(body["id"], str)
    assert isinstance(body["tenant_id"], str)
    assert body["folder_id"] == "FOLDER"
    assert body["folder_name"] == "Policies"


@pytest.mark.asyncio
async def test_create_drive_folder_requires_sheets_connector(client, db):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    await _enable_drive_rag(db, tenant.id)
    # No Sheets connector added
    resp = await client.post(
        "/api/v1/drive-folders",
        json={"folder_id_or_url": "https://drive.google.com/drive/folders/FOLDER"},
        headers=make_auth_headers(user),
    )
    assert resp.status_code == 400
    assert "sheets" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_delete_drive_folder(client, db):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    await _enable_drive_rag(db, tenant.id)
    folder = DriveFolder(tenant_id=tenant.id, folder_id="X", folder_name="F", created_by=user.id)
    db.add(folder)
    await db.commit()
    resp = await client.delete(f"/api/v1/drive-folders/{folder.id}", headers=make_auth_headers(user))
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_patch_toggle_enabled(client, db):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    await _enable_drive_rag(db, tenant.id)
    folder = DriveFolder(tenant_id=tenant.id, folder_id="X", folder_name="F", created_by=user.id)
    db.add(folder)
    await db.commit()
    resp = await client.patch(
        f"/api/v1/drive-folders/{folder.id}",
        json={"is_enabled": False},
        headers=make_auth_headers(user),
    )
    assert resp.status_code == 200
    assert resp.json()["is_enabled"] is False


@pytest.mark.asyncio
async def test_sync_endpoint_enqueues_task(client, db):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    await _enable_drive_rag(db, tenant.id)
    folder = DriveFolder(tenant_id=tenant.id, folder_id="X", folder_name="F", created_by=user.id)
    db.add(folder)
    await db.commit()
    with patch("app.api.v1.drive_folders.drive_rag_sync_folder.delay") as m:
        resp = await client.post(f"/api/v1/drive-folders/{folder.id}/sync", headers=make_auth_headers(user))
    assert resp.status_code == 202
    m.assert_called_once_with(str(folder.id), tenant_id=str(tenant.id))


@pytest.mark.asyncio
async def test_status_endpoint(client, db):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    await _enable_drive_rag(db, tenant.id)
    folder = DriveFolder(tenant_id=tenant.id, folder_id="X", folder_name="F", created_by=user.id)
    db.add(folder)
    await db.commit()
    resp = await client.get(f"/api/v1/drive-folders/{folder.id}/status", headers=make_auth_headers(user))
    assert resp.status_code == 200
    body = resp.json()
    assert body["sync_status"] == "idle"
    assert isinstance(body["id"], str)
