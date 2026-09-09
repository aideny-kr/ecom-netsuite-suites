import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CursorState(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "cursor_states"
    __table_args__ = (UniqueConstraint("connection_id", "object_type", name="uq_cursor_states_conn_obj"),)

    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connections.id"), nullable=False, index=True
    )
    object_type: Mapped[str] = mapped_column(String(100), nullable=False)
    cursor_value: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvidencePack(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "evidence_packs"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    pack_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=True)
    storage_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_format: Mapped[str | None] = mapped_column(String(50), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Schedule(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A recurring job. Pre-Slice-2 rows are opaque parameter bags
    (``schedule_type`` in {sync, report, recon}); a Scheduled Job (Slice 2,
    ``schedule_type == "job"``) additionally carries a plain-language
    ``instruction`` compiled into an allow-listed ``plan_json`` (spec §B1) — see
    ``app.services.jobs.registry`` for the step allow-list and
    ``docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md``
    Part B for the full data model this maps.
    """

    __tablename__ = "schedules"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    schedule_type: Mapped[str] = mapped_column(String(100), nullable=False)
    cron_expression: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    parameters: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # ---- Scheduled Job fields (migration 100_scheduled_jobs, spec §B1) --------
    # The instruction is the source of truth; plan_json is what actually runs
    # (compiled from it, never hand-edited — spec §0 "Out of scope now: editing a
    # compiled plan by hand").
    instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    plan_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    plan_status: Mapped[str | None] = mapped_column(Text, nullable=True)  # draft|pending_approval|approved
    # A recompiled plan (instruction edit) lands here, pending a person's approval
    # (diff shown) before it replaces plan_json — never applied automatically.
    pending_plan_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    pending_plan_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str] = mapped_column(Text, default="UTC", nullable=False)
    delivery_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    budget_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # {bytes_scanned, seconds, usd}
    catch_up: Mapped[str] = mapped_column(Text, default="once", nullable=False)  # once|skip
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # migration 101_schedule_created_via, Task 5 residual: "chat" | "page" |
    # "seed" -- how this schedule was created (spec §B6's list-page sub-line).
    # `None` for a row created before this field existed.
    created_via: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # migration 102_schedule_retry_job, delta gate item 1: the one 15-minutes-
    # later retry (spec §B4) is an explicit reference to its pre-created
    # `jobs` row -- set by `run_schedule_now`'s retry-then-pause branch at
    # SCHEDULING time, read by `_claim_due_schedules` to decide `attempt`
    # (2 if set, else 1) and to reuse that SAME row rather than a JSON query
    # ordered by `started_at` (see the migration's own docstring for why that
    # broke). Cleared in the SAME transaction the sweep claims it in.
    retry_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
