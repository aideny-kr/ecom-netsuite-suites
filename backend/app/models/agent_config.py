"""Per-tenant agent configuration and performance metrics."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AgentConfig(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "agent_configs"
    __table_args__ = (UniqueConstraint("tenant_id", "agent_id", name="uq_agent_configs_tenant_agent"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    override_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    instructions_updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
