from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TenantMemoryEdge(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A directed, named relationship between two concepts in a tenant's memory graph."""

    __tablename__ = "tenant_memory_edge"
    # The (tenant, source, target, relation) tuple is the backfill edge idempotency
    # key — re-runs use on_conflict_do_nothing against it so no duplicate edge mints.
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_concept_id",
            "target_concept_id",
            "relation",
            name="uq_tenant_memory_edge",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_concept_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenant_memory_concept.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_concept_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenant_memory_concept.id", ondelete="CASCADE"),
        nullable=False,
    )
    relation: Mapped[str] = mapped_column(String(100), nullable=False)
    review_state: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
