from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.tenant import Tenant
    from app.models.user import User


class Workspace(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "workspaces"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    tenant: Mapped["Tenant"] = relationship("Tenant")
    creator: Mapped["User"] = relationship("User", foreign_keys=[created_by])
    files: Mapped[list["WorkspaceFile"]] = relationship(
        "WorkspaceFile", back_populates="workspace", cascade="all, delete-orphan"
    )
    changesets: Mapped[list["WorkspaceChangeSet"]] = relationship(
        "WorkspaceChangeSet", back_populates="workspace", cascade="all, delete-orphan"
    )
    runs: Mapped[list["WorkspaceRun"]] = relationship(
        "WorkspaceRun", back_populates="workspace", cascade="all, delete-orphan"
    )


class WorkspaceFile(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "workspace_files"
    __table_args__ = (UniqueConstraint("workspace_id", "path", name="uq_workspace_files_workspace_path"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_directory: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    netsuite_file_id: Mapped[str | None] = mapped_column(String(50), nullable=True)

    workspace: Mapped["Workspace"] = relationship("Workspace", back_populates="files")


class WorkspaceChangeSet(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "workspace_changesets"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="draft", nullable=False)
    proposed_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    applied_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()", nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    workspace: Mapped["Workspace"] = relationship("Workspace", back_populates="changesets")
    patches: Mapped[list["WorkspacePatch"]] = relationship(
        "WorkspacePatch", back_populates="changeset", cascade="all, delete-orphan"
    )


class WorkspacePatch(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "workspace_patches"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    changeset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_changesets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)  # modify, create, delete
    unified_diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    baseline_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    apply_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()", nullable=False)

    changeset: Mapped["WorkspaceChangeSet"] = relationship("WorkspaceChangeSet", back_populates="patches")


class WorkspaceRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "workspace_runs"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    changeset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspace_changesets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    run_type: Mapped[str] = mapped_column(String(50), nullable=False)  # sdf_validate | jest_unit_test
    status: Mapped[str] = mapped_column(
        String(50), default="queued", nullable=False
    )  # queued | running | passed | failed | error
    triggered_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    workspace: Mapped["Workspace"] = relationship("Workspace", back_populates="runs")
    trigger_user: Mapped["User"] = relationship("User", foreign_keys=[triggered_by])
    artifacts: Mapped[list["WorkspaceArtifact"]] = relationship(
        "WorkspaceArtifact", back_populates="run", cascade="all, delete-orphan"
    )


class WorkspaceArtifact(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "workspace_artifacts"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # stdout | stderr | report_json | coverage_json
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sha256_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()", nullable=False)

    run: Mapped["WorkspaceRun"] = relationship("WorkspaceRun", back_populates="artifacts")
