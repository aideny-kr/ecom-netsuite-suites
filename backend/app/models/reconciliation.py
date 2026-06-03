"""SQLAlchemy models for reconciliation runs and results."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ReconciliationRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "reconciliation_runs"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=True)
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    subsidiary_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    total_payouts: Mapped[int] = mapped_column(default=0, nullable=False)
    total_deposits: Mapped[int] = mapped_column(default=0, nullable=False)
    matched_count: Mapped[int] = mapped_column(default=0, nullable=False)
    exception_count: Mapped[int] = mapped_column(default=0, nullable=False)
    unmatched_count: Mapped[int] = mapped_column(default=0, nullable=False)
    total_variance: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0"), nullable=False)
    # Per-bucket rollup counts for the runs-list view (R2a). Computed at write-time
    # from the persisted ReconciliationResult.bucket values.
    matches_count: Mapped[int] = mapped_column(default=0, server_default="0", nullable=False)
    rules_count: Mapped[int] = mapped_column(default=0, server_default="0", nullable=False)
    auto_classifications_count: Mapped[int] = mapped_column(default=0, server_default="0", nullable=False)
    needs_review_count: Mapped[int] = mapped_column(default=0, server_default="0", nullable=False)
    parameters: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ReconciliationResult(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "reconciliation_results"
    __table_args__ = (Index("ix_reconciliation_results_run_bucket", "run_id", "bucket"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reconciliation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    payout_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("payouts.id"), nullable=True)
    deposit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("netsuite_postings.id"), nullable=True
    )
    match_type: Mapped[str] = mapped_column(String(50), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    # Persisted four-bucket classification (R2a). Computed at write-time via
    # classify() with the tenant's materiality thresholds; the read-side and the
    # SQL twin select on this column instead of recomputing.
    bucket: Mapped[str] = mapped_column(
        String(50), default="needs_review", server_default="needs_review", nullable=False
    )
    stripe_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    netsuite_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    variance_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0"), nullable=False)
    variance_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    variance_explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    match_rule: Mapped[str | None] = mapped_column(String(255), nullable=True)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
