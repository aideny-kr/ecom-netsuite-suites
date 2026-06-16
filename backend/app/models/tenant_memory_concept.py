from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TenantMemoryConcept(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    A single plain-English business concept in a tenant's memory graph.

    Overlay over the existing TenantLearnedRule + TenantQueryPattern learning
    tables. Carries a trust spine (``review_state`` + ``confidence`` +
    ``confirmed_by``) so only customer-confirmed concepts ever reach the agent
    prompt, and rejected ones stop driving answers.
    """

    __tablename__ = "tenant_memory_concept"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    concept_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    review_state: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    # Reserved for the ② live-capture provenance path (deferred) — which session/
    # message a concept was captured from. Not yet written; do not remove.
    origin_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    origin_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenant_memory_concept.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Reserved for the ② usage-ranking path (deferred) — how often / when last a
    # confirmed concept was injected. Not yet incremented; do not remove.
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
