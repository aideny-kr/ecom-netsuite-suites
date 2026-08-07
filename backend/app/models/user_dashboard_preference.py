import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class UserDashboardPreference(Base, UUIDPrimaryKeyMixin):
    """A user's chosen dashboard wallpaper — one active selection per (tenant, user),
    which is EITHER a report (a pinned snapshot) OR a series (rolling-period Stage 1,
    Task 4: a tracking selection that follows the series' newest report) — never both.

    The published set (reports.dashboard_pinned_at) is workspace-wide; this table
    holds the personal choice. Both `report_id` and `series_id` are nullable with
    `ON DELETE SET NULL`: deleting the selected report/series tombstones this row
    (the respective column -> NULL) instead of removing it, so the read side (Task 2 /
    Task 4) can tell "chose it, then it was deleted" apart from "never chosen" and show
    the fallback notice. Handled entirely by each FK's ON DELETE action, not by
    application code. `ck_user_dashboard_preference_one_selection` (migration 094)
    enforces "at most one of report_id/series_id is non-null" at the DB level — the
    API's own 400 for a PUT supplying both (or neither) is belt; this is suspenders,
    and also the only thing preventing a stray direct-ORM write from splitting a row
    across both selection kinds at once.
    """

    __tablename__ = "user_dashboard_preferences"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_user_dashboard_preference_tenant_user"),
        CheckConstraint("report_id IS NULL OR series_id IS NULL", name="ck_user_dashboard_preference_one_selection"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reports.id", ondelete="SET NULL"), nullable=True
    )
    # Rolling-period Stage 1 (migration 094): the tracking counterpart to report_id —
    # set together never, see the CHECK constraint above.
    series_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("report_series.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
