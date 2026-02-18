"""SuiteScript sync state tracking model."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.connection import Connection
    from app.models.workspace import Workspace


class ScriptSyncState(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Tracks the state of SuiteScript file sync for each tenant.

    One row per tenant (unique constraint on tenant_id).
    Status flow: pending → in_progress → completed / failed
    """

    __tablename__ = "script_sync_states"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, unique=True, index=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connections.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending", server_default="pending")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_files_loaded: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    discovered_file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failed_files_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    workspace: Mapped["Workspace"] = relationship("Workspace")
    connection: Mapped["Connection | None"] = relationship("Connection")
