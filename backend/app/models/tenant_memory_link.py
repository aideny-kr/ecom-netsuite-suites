from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TenantMemoryLink(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Evidence link: which source learning row(s) a concept was extracted from.

    ``source_id`` intentionally has NO cross-table FK (it points at either
    ``tenant_learned_rules`` or ``tenant_query_patterns``); referential
    integrity is enforced in the service layer. The unique constraint on
    (tenant_id, source_table, source_id) is the backfill idempotency key.
    """

    __tablename__ = "tenant_memory_link"
    __table_args__ = (UniqueConstraint("tenant_id", "source_table", "source_id", name="uq_tenant_memory_link_source"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    concept_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenant_memory_concept.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_table: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
