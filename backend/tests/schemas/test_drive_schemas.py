import datetime as dt

import pytest

from app.schemas.drive import DriveFolderCreate, DriveFolderResponse, DriveFolderStatus


def test_create_accepts_url():
    m = DriveFolderCreate(folder_id_or_url="https://drive.google.com/drive/folders/ABC")
    assert m.folder_id_or_url.startswith("https://")


def test_create_rejects_empty():
    with pytest.raises(ValueError):
        DriveFolderCreate(folder_id_or_url="   ")


def test_response_requires_string_id():
    m = DriveFolderResponse(
        id="11111111-2222-3333-4444-555555555555",
        tenant_id="11111111-2222-3333-4444-555555555555",
        folder_id="X",
        folder_name="F",
        is_enabled=True,
        sync_status="idle",
        last_synced_at=None,
        last_sync_error=None,
        chunk_count=0,
        file_count=0,
        created_at=dt.datetime.now(dt.timezone.utc),
    )
    assert isinstance(m.id, str)
    assert isinstance(m.tenant_id, str)


def test_status_requires_valid_sync_status():
    with pytest.raises(ValueError):
        DriveFolderStatus(
            id="x",
            sync_status="banana",  # invalid
            last_synced_at=None,
            last_sync_error=None,
            chunk_count=0,
            file_count=0,
        )
