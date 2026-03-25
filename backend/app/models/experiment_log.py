"""Experiment log for autonomous query improvement tracking."""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ExperimentLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "experiment_log"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    dialect: Mapped[str] = mapped_column(String(20), nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    test_query: Mapped[str] = mapped_column(Text, nullable=False)
    generated_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_successfully: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    score_accuracy: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_syntax: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_efficiency: Mapped[float | None] = mapped_column(Float, nullable=True)
    experiment_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    decision: Mapped[str | None] = mapped_column(String(10), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
