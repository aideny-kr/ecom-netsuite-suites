import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class UserDashboardPreference(Base, UUIDPrimaryKeyMixin):
    """A user's chosen dashboard wallpaper — one active report per (tenant, user).

    The published set (reports.dashboard_pinned_at) is workspace-wide; this
    table holds the personal choice. `report_id` cascades on report delete so
    an unpublished-then-deleted report simply drops the selection (Task 2's
    read-side falls back to the most recently published report; deletion is
    handled entirely by the FK, not by application code).
    """

    __tablename__ = "user_dashboard_preferences"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_user_dashboard_preference_tenant_user"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reports.id", ondelete="CASCADE"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
