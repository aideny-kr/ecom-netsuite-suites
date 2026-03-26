"""Eval cases for autonomous query improvement loop."""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EvalCase(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "eval_cases"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    dialect: Mapped[str] = mapped_column(String(20), nullable=False)
    expected_keywords: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'"))
    source: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'organic'"))
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    generated_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    times_tested: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
