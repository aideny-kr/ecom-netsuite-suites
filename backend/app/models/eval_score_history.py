"""Eval score history — tracks nightly improvement loop results over time."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class EvalScoreHistory(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "eval_score_history"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    # Migration 057 records creation only; score history has no updated_at column.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    dialect: Mapped[str] = mapped_column(String(20), nullable=False)
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    kept: Mapped[int] = mapped_column(Integer, nullable=False)
    reverted: Mapped[int] = mapped_column(Integer, nullable=False)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    errors: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_composite_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
