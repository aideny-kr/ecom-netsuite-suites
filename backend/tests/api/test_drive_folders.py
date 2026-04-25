from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.core.encryption import encrypt_credentials
from app.models.drive import DriveFile, DriveFolder
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


# ---------------------------------------------------------------------------
# GET /drive-folders/files — typeahead for the `#` mention picker
# ---------------------------------------------------------------------------


async def _add_file(
    db,
    *,
    tenant_id,
    folder_id,
    drive_file_id: str,
    name: str,
    mime_type: str = "application/vnd.google-apps.document",
) -> DriveFile:
    now = datetime.now(timezone.utc)
    f = DriveFile(
        tenant_id=tenant_id,
        folder_id=folder_id,
        drive_file_id=drive_file_id,
        name=name,
        mime_type=mime_type,
        web_view_link=f"https://docs.google.com/document/d/{drive_file_id}/edit",
        modified_time=now,
        indexed_at=now,
        chunk_count=3,
    )
    db.add(f)
    await db.commit()
    await db.refresh(f)
    return f


@pytest.mark.asyncio
async def test_list_drive_files_empty(client, db):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    await _enable_drive_rag(db, tenant.id)
    resp = await client.get("/api/v1/drive-folders/files", headers=make_auth_headers(user))
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_drive_files_returns_indexed_files_sorted_by_name(client, db):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    await _enable_drive_rag(db, tenant.id)
    folder = DriveFolder(tenant_id=tenant.id, folder_id="F", folder_name="Policies", created_by=user.id)
    folder.is_enabled = True
    db.add(folder)
    await db.commit()
    await _add_file(db, tenant_id=tenant.id, folder_id=folder.id, drive_file_id="B", name="Benefits")
    await _add_file(db, tenant_id=tenant.id, folder_id=folder.id, drive_file_id="A", name="Absence Policy")
    await _add_file(db, tenant_id=tenant.id, folder_id=folder.id, drive_file_id="C", name="Code of Conduct")

    resp = await client.get("/api/v1/drive-folders/files", headers=make_auth_headers(user))
    assert resp.status_code == 200
    body = resp.json()
    assert [f["name"] for f in body] == ["Absence Policy", "Benefits", "Code of Conduct"]
    # Each entry exposes string IDs + folder context + chunk count for the picker.
    for item in body:
        assert isinstance(item["id"], str)
        assert isinstance(item["drive_file_id"], str)
        assert item["folder_name"] == "Policies"
        assert item["chunk_count"] == 3
        assert item["web_view_link"].startswith("https://docs.google.com/document/")


@pytest.mark.asyncio
async def test_list_drive_files_filters_by_q(client, db):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    await _enable_drive_rag(db, tenant.id)
    folder = DriveFolder(tenant_id=tenant.id, folder_id="F", folder_name="Policies", created_by=user.id)
    db.add(folder)
    await db.commit()
    await _add_file(db, tenant_id=tenant.id, folder_id=folder.id, drive_file_id="R", name="Returns Policy")
    await _add_file(db, tenant_id=tenant.id, folder_id=folder.id, drive_file_id="S", name="Shipping FAQ")

    # Case-insensitive substring match against name
    resp = await client.get("/api/v1/drive-folders/files?q=ret", headers=make_auth_headers(user))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["name"] == "Returns Policy"


@pytest.mark.asyncio
async def test_list_drive_files_excludes_disabled_folders(client, db):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    await _enable_drive_rag(db, tenant.id)
    enabled = DriveFolder(tenant_id=tenant.id, folder_id="E", folder_name="Enabled", created_by=user.id)
    disabled = DriveFolder(tenant_id=tenant.id, folder_id="D", folder_name="Disabled", created_by=user.id)
    disabled.is_enabled = False
    db.add_all([enabled, disabled])
    await db.commit()
    await _add_file(db, tenant_id=tenant.id, folder_id=enabled.id, drive_file_id="ea", name="EnabledDoc")
    await _add_file(db, tenant_id=tenant.id, folder_id=disabled.id, drive_file_id="da", name="DisabledDoc")

    resp = await client.get("/api/v1/drive-folders/files", headers=make_auth_headers(user))
    body = resp.json()
    names = [f["name"] for f in body]
    assert "EnabledDoc" in names
    assert "DisabledDoc" not in names


@pytest.mark.asyncio
async def test_list_drive_files_tenant_isolation(client, db):
    """Tenant A must not see Tenant B's files."""
    tenant_a = await create_test_tenant(db)
    tenant_b = await create_test_tenant(db)
    user_a, _ = await create_test_user(db, tenant_a)
    user_b, _ = await create_test_user(db, tenant_b)
    await _enable_drive_rag(db, tenant_a.id)
    await _enable_drive_rag(db, tenant_b.id)

    folder_a = DriveFolder(tenant_id=tenant_a.id, folder_id="FA", folder_name="A", created_by=user_a.id)
    folder_b = DriveFolder(tenant_id=tenant_b.id, folder_id="FB", folder_name="B", created_by=user_b.id)
    db.add_all([folder_a, folder_b])
    await db.commit()
    await _add_file(db, tenant_id=tenant_a.id, folder_id=folder_a.id, drive_file_id="a", name="OnlyA")
    await _add_file(db, tenant_id=tenant_b.id, folder_id=folder_b.id, drive_file_id="b", name="OnlyB")

    resp_a = await client.get("/api/v1/drive-folders/files", headers=make_auth_headers(user_a))
    resp_b = await client.get("/api/v1/drive-folders/files", headers=make_auth_headers(user_b))
    assert [f["name"] for f in resp_a.json()] == ["OnlyA"]
    assert [f["name"] for f in resp_b.json()] == ["OnlyB"]


@pytest.mark.asyncio
async def test_list_drive_files_caps_at_limit(client, db):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    await _enable_drive_rag(db, tenant.id)
    folder = DriveFolder(tenant_id=tenant.id, folder_id="F", folder_name="F", created_by=user.id)
    db.add(folder)
    await db.commit()
    for i in range(25):
        await _add_file(db, tenant_id=tenant.id, folder_id=folder.id, drive_file_id=f"f{i:02d}", name=f"File {i:02d}")

    resp = await client.get("/api/v1/drive-folders/files?limit=5", headers=make_auth_headers(user))
    assert resp.status_code == 200
    assert len(resp.json()) == 5


@pytest.mark.asyncio
async def test_list_drive_files_requires_drive_rag_flag(client, db):
    """Feature flag gates the endpoint — same as the other drive-folders routes."""
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    # drive_rag NOT enabled
    resp = await client.get("/api/v1/drive-folders/files", headers=make_auth_headers(user))
    assert resp.status_code == 403
