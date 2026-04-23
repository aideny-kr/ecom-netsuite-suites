"""Test the Drive RAG SQLAlchemy models exist with required columns."""

from sqlalchemy import inspect

from app.models.drive import DriveChunk, DriveFile, DriveFolder


def test_drive_folder_has_required_columns():
    cols = {c.name for c in inspect(DriveFolder).columns}
    assert {"id", "tenant_id", "folder_id", "folder_name", "is_enabled",
            "sync_status", "last_synced_at", "last_sync_error", "created_by",
            "created_at", "updated_at"} <= cols


def test_drive_file_has_required_columns():
    cols = {c.name for c in inspect(DriveFile).columns}
    assert {"id", "tenant_id", "folder_id", "drive_file_id", "name",
            "mime_type", "web_view_link", "modified_time", "indexed_at",
            "chunk_count"} <= cols


def test_drive_chunk_has_required_columns():
    mapper = inspect(DriveChunk)
    # DB column names (metadata, not metadata_ — "metadata" is the physical column)
    cols = {c.name for c in mapper.columns}
    assert {"id", "tenant_id", "file_id", "chunk_index", "content",
            "token_count", "embedding", "metadata"} <= cols
    # Python attribute is metadata_ (the underscore avoids SQLAlchemy's reserved .metadata)
    attr_keys = {a.key for a in mapper.attrs}
    assert "metadata_" in attr_keys


def test_drive_folder_tablename():
    assert DriveFolder.__tablename__ == "drive_folders"
    assert DriveFile.__tablename__ == "drive_files"
    assert DriveChunk.__tablename__ == "drive_chunks"
