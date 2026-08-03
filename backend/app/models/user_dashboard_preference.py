import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class UserDashboardPreference(Base, UUIDPrimaryKeyMixin):
    """A user's chosen dashboard wallpaper — one active report per (tenant, user).

    The published set (reports.dashboard_pinned_at) is workspace-wide; this
    table holds the personal choice. `report_id` is nullable with
    `ON DELETE SET NULL`: deleting the selected report tombstones this row
    (report_id -> NULL) instead of removing it, so the read side (Task 2) can
    tell "chose it, then it was deleted" apart from "never chosen" and show
    the fallback notice for both deleted and unpublished selections. Handled
    entirely by the FK's ON DELETE action, not by application code.
    """

    __tablename__ = "user_dashboard_preferences"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_user_dashboard_preference_tenant_user"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reports.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
