from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.tenant import Tenant
    from app.models.user import User


class NetSuiteMetadata(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Stores discovered NetSuite custom field definitions, record types, and org hierarchy.

    Versioned per tenant — each full discovery run creates a new record.
    Raw JSON blobs are stored here; prompt template sections and RAG docs
    are derived downstream.
    """

    __tablename__ = "netsuite_metadata"
    __table_args__ = (UniqueConstraint("tenant_id", "version", name="uq_netsuite_metadata_tenant_version"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    # ── Discovery result blobs ──────────────────────────────────────
    transaction_body_fields: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    transaction_column_fields: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    entity_custom_fields: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    item_custom_fields: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    custom_record_types: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    custom_lists: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    subsidiaries: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    departments: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    classifications: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    locations: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # ── Discovery tracking ──────────────────────────────────────────
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    discovery_errors: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    query_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_fields_discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── Relationships ───────────────────────────────────────────────
    tenant: Mapped["Tenant"] = relationship("Tenant")
    discoverer: Mapped["User | None"] = relationship("User", foreign_keys=[discovered_by])
