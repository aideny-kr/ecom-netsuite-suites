from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DriveFolder(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Tenant-registered Google Drive folder to index for RAG."""

    __tablename__ = "drive_folders"
    __table_args__ = (
        UniqueConstraint("tenant_id", "folder_id", name="uq_drive_folder_tenant_folder"),
        Index("ix_drive_folders_tenant_enabled", "tenant_id", "is_enabled"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    folder_id: Mapped[str] = mapped_column(String(128), nullable=False)
    folder_name: Mapped[str] = mapped_column(String(512), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sync_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="idle"
    )  # idle | syncing | success | error
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    files: Mapped[list["DriveFile"]] = relationship(back_populates="folder", cascade="all, delete-orphan")


class DriveFile(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Per-file indexing metadata. One row per indexed Drive file."""

    __tablename__ = "drive_files"
    __table_args__ = (
        UniqueConstraint("tenant_id", "drive_file_id", name="uq_drive_file_tenant_file"),
        Index("ix_drive_files_folder", "folder_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    folder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drive_folders.id", ondelete="CASCADE"), nullable=False
    )
    drive_file_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    web_view_link: Mapped[str] = mapped_column(Text, nullable=False)
    modified_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_extract_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    folder: Mapped["DriveFolder"] = relationship(back_populates="files")
    chunks: Mapped[list["DriveChunk"]] = relationship(back_populates="file", cascade="all, delete-orphan")


class DriveChunk(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Embedded chunk of a Drive file. Tenant-scoped."""

    __tablename__ = "drive_chunks"
    __table_args__ = (
        Index("ix_drive_chunks_tenant", "tenant_id"),
        Index("ix_drive_chunks_file", "file_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drive_files.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding = mapped_column(Vector(1024), nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)

    file: Mapped["DriveFile"] = relationship(back_populates="chunks")
