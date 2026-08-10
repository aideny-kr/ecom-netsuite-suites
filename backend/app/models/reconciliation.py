"""SQLAlchemy models for reconciliation runs and results."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Numeric, String, Text
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

    # Negative-label capture. `rejected` was a declared ResultStatus and already
    # terminal, but nothing ever set it — so wrong matches left no trace and the
    # autonomy envelope's false-positive rate was unmeasurable by construction.
    # See services/reconciliation/recon_reject.py for why each column exists.
    rejected_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reject_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Snapshotted at decision time, never recomputed: the envelope's admission
    # rules will change, and replaying today's rules over yesterday's rows would
    # silently re-baseline the metric every time they move.
    envelope_eligible_at_decision: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Eligible AND rejected for a reason that means the matcher was wrong.
    # `not_actionable` is excluded on purpose — the match was correct, and
    # counting it would report operational friction as model error.
    # Attribute renamed, DB column deliberately NOT. The old name asserts a
    # false-POSITIVE rate — P(auto-post | actually wrong) — whose denominator covers
    # rows the envelope never selected and which is therefore unidentifiable. What the
    # autonomy decision needs is 1 - precision, the false DISCOVERY rate. Code should
    # say the true thing, so the attribute is renamed.
    #
    # The COLUMN keeps its old name because renaming it breaks the deploy. Migrations
    # run from the CI runner BEFORE any container is replaced (deploy.yml: `alembic
    # upgrade head` then `docker compose up -d --no-deps`), and SQLAlchemy emits an
    # explicit column list rather than SELECT *, so the still-running previous image
    # would 500 on every recon read for the length of the restart — worse in
    # production, where the worker is replaced only after the backend health check.
    # Verified by compiling both models against a migrated scratch DB: the old
    # mapping raises UndefinedColumnError.
    #
    # "Zero rows" does not help: this is code/schema skew, not a data migration. The
    # rename is safe only as expand/contract (add, dual-write, backfill, drop a
    # release later), which is not worth a release cycle for a cosmetic column name.
    counts_as_envelope_error: Mapped[bool | None] = mapped_column("counts_as_false_positive", Boolean, nullable=True)


# Proposal statuses that occupy the one-active-per-result slot (partial unique
# index in migration 089). superseded/rejected rows are retained history.
ACTIVE_PROPOSAL_STATUSES = ("proposed", "approved", "posting", "posted", "post_failed")


class ReconResolutionProposal(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "recon_resolution_proposals"
    __table_args__ = (
        Index(
            "ix_recon_resolution_proposals_run_group",
            "run_id",
            "root_cause",
            "action",
            "booking_vehicle",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reconciliation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reconciliation_results.id", ondelete="CASCADE"), nullable=False
    )
    root_cause: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    booking_vehicle: Mapped[str] = mapped_column(String(50), nullable=False)
    group_key: Mapped[str] = mapped_column(String(160), nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="planner", server_default="planner", nullable=False)
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    proposed_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0"), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    above_materiality: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="proposed", server_default="proposed", nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    netsuite_record_refs: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    # Cross-run double-posting guard key, denormalized from result.evidence.
    charge_source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
