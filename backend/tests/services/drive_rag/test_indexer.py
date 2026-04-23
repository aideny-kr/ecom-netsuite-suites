from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.drive import DriveChunk, DriveFile, DriveFolder
from app.services.drive_rag.indexer import sync_folder
from tests.conftest import create_test_tenant, create_test_user


@pytest.mark.asyncio
async def test_sync_folder_indexes_new_files(db):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    folder = DriveFolder(
        tenant_id=tenant.id,
        folder_id="FID",
        folder_name="Old Name",
        created_by=user.id,
    )
    db.add(folder)
    await db.commit()
    await db.refresh(folder)

    drive_files = [
        {
            "id": "f1",
            "name": "Returns Policy",
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "2026-04-20T10:00:00Z",
            "webViewLink": "https://docs.google.com/document/d/f1/edit",
        }
    ]
    with patch(
        "app.services.drive_rag.indexer.drive_client.get_folder_metadata",
        new=AsyncMock(return_value={"id": "FID", "name": "New Name", "mimeType": "x"}),
    ), patch(
        "app.services.drive_rag.indexer.drive_client.list_folder_files",
        new=AsyncMock(return_value=drive_files),
    ), patch(
        "app.services.drive_rag.indexer.extractors.extract_by_mime",
        new=AsyncMock(return_value="Return policy: 30 days."),
    ), patch(
        "app.services.drive_rag.indexer.embed_texts",
        new=AsyncMock(return_value=[[0.01] * 1024]),
    ):
        result = await sync_folder(db, folder_id=folder.id, credentials={})

    assert result["files_indexed"] == 1
    assert result["files_deleted"] == 0

    await db.refresh(folder)
    assert folder.folder_name == "New Name"
    assert folder.sync_status == "success"
    assert folder.last_synced_at is not None

    drive_file = (
        await db.execute(select(DriveFile).where(DriveFile.drive_file_id == "f1"))
    ).scalar_one()
    assert drive_file.chunk_count == 1
    chunks = (
        (await db.execute(select(DriveChunk).where(DriveChunk.file_id == drive_file.id)))
        .scalars()
        .all()
    )
    assert len(chunks) == 1
    assert chunks[0].tenant_id == tenant.id


@pytest.mark.asyncio
async def test_sync_folder_skips_unchanged_files(db):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    folder = DriveFolder(
        tenant_id=tenant.id, folder_id="FID", folder_name="F", created_by=user.id
    )
    db.add(folder)
    await db.flush()
    existing = DriveFile(
        tenant_id=tenant.id,
        folder_id=folder.id,
        drive_file_id="f1",
        name="Old",
        mime_type="text/plain",
        web_view_link="https://x",
        modified_time=datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc),
        indexed_at=datetime(2026, 4, 20, 10, 5, tzinfo=timezone.utc),
        chunk_count=1,
    )
    db.add(existing)
    await db.commit()

    drive_files_unchanged = [
        {
            "id": "f1",
            "name": "Old",
            "mimeType": "text/plain",
            "modifiedTime": "2026-04-20T10:00:00Z",
            "webViewLink": "https://x",
        }
    ]
    extract_mock = AsyncMock(return_value="body")
    with patch(
        "app.services.drive_rag.indexer.drive_client.get_folder_metadata",
        new=AsyncMock(return_value={"id": "FID", "name": "F", "mimeType": "x"}),
    ), patch(
        "app.services.drive_rag.indexer.drive_client.list_folder_files",
        new=AsyncMock(return_value=drive_files_unchanged),
    ), patch(
        "app.services.drive_rag.indexer.extractors.extract_by_mime", new=extract_mock
    ), patch(
        "app.services.drive_rag.indexer.embed_texts",
        new=AsyncMock(return_value=[[0.0] * 1024]),
    ):
        result = await sync_folder(db, folder_id=folder.id, credentials={})

    assert result["files_indexed"] == 0
    assert extract_mock.call_count == 0


@pytest.mark.asyncio
async def test_sync_folder_deletes_missing_files(db):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    folder = DriveFolder(
        tenant_id=tenant.id, folder_id="FID", folder_name="F", created_by=user.id
    )
    db.add(folder)
    await db.flush()
    stale = DriveFile(
        tenant_id=tenant.id,
        folder_id=folder.id,
        drive_file_id="deleted",
        name="Deleted",
        mime_type="text/plain",
        web_view_link="https://x",
        modified_time=datetime.now(timezone.utc),
        indexed_at=datetime.now(timezone.utc),
        chunk_count=0,
    )
    db.add(stale)
    await db.commit()

    with patch(
        "app.services.drive_rag.indexer.drive_client.get_folder_metadata",
        new=AsyncMock(return_value={"id": "FID", "name": "F", "mimeType": "x"}),
    ), patch(
        "app.services.drive_rag.indexer.drive_client.list_folder_files",
        new=AsyncMock(return_value=[]),
    ):
        result = await sync_folder(db, folder_id=folder.id, credentials={})
    assert result["files_deleted"] == 1
    remaining = (
        (await db.execute(select(DriveFile).where(DriveFile.folder_id == folder.id)))
        .scalars()
        .all()
    )
    assert remaining == []


@pytest.mark.asyncio
async def test_sync_folder_records_extract_error_and_continues(db):
    tenant = await create_test_tenant(db)
    user, _ = await create_test_user(db, tenant)
    folder = DriveFolder(
        tenant_id=tenant.id, folder_id="FID", folder_name="F", created_by=user.id
    )
    db.add(folder)
    await db.commit()
    await db.refresh(folder)

    drive_files = [
        {
            "id": "ok",
            "name": "OK",
            "mimeType": "text/plain",
            "modifiedTime": "2026-04-22T00:00:00Z",
            "webViewLink": "https://x",
        },
        {
            "id": "bad",
            "name": "Bad",
            "mimeType": "application/pdf",
            "modifiedTime": "2026-04-22T00:00:00Z",
            "webViewLink": "https://y",
        },
    ]

    async def _extract(*, credentials, file_id, mime_type):
        if file_id == "bad":
            raise RuntimeError("extract fail")
        return "ok body"

    with patch(
        "app.services.drive_rag.indexer.drive_client.get_folder_metadata",
        new=AsyncMock(return_value={"id": "FID", "name": "F", "mimeType": "x"}),
    ), patch(
        "app.services.drive_rag.indexer.drive_client.list_folder_files",
        new=AsyncMock(return_value=drive_files),
    ), patch(
        "app.services.drive_rag.indexer.extractors.extract_by_mime",
        new=AsyncMock(side_effect=_extract),
    ), patch(
        "app.services.drive_rag.indexer.embed_texts",
        new=AsyncMock(return_value=[[0.0] * 1024]),
    ):
        result = await sync_folder(db, folder_id=folder.id, credentials={})

    assert result["files_indexed"] == 1
    assert result["files_failed"] == 1
    bad_row = (
        await db.execute(select(DriveFile).where(DriveFile.drive_file_id == "bad"))
    ).scalar_one()
    assert bad_row.last_extract_error is not None
