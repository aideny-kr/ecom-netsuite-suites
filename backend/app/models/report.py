import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    spec_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    rendered_html: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft", server_default="draft")
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # Slice A (live-dashboard reports): the captured refresh recipe — the LLM's compose
    # sections VERBATIM + per-result_id {tool, params, connection_id} server-captured from
    # executed tool calls. NULL = snapshot-only report (no backfill; refresh unavailable).
    recipe_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Slice B: the DB-derived refresh debounce stamp — set at ATTEMPT time (a failed
    # refresh also consumes the ~5 min window; quota protection against hammering a
    # dead OAuth connection). NULL = never refreshed.
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Slice C: the USER-CHOSEN sweep interval (off|hourly|daily) — never overwritten by
    # the failure ladder (backoff is derived in the sweep from refresh_failure_count).
    # 'daily' default is inert without recipe_json (the sweep predicate requires it).
    auto_refresh: Mapped[str] = mapped_column(Text, nullable=False, default="daily", server_default="daily")
    # Slice C failure ladder: consecutive FAILED auto-refreshes (sweep-owned — manual
    # refresh and debounce/supersede skips never touch it; success resets to 0).
    refresh_failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Slice C: stamped when ~7 consecutive failures pause the sweep for this report;
    # cleared ONLY by the user's explicit resume (a later success never clears it).
    auto_refresh_paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_drive_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
