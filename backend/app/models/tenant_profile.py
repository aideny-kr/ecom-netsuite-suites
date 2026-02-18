from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.tenant import Tenant
    from app.models.user import User


class TenantProfile(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "tenant_profiles"
    __table_args__ = (UniqueConstraint("tenant_id", "version", name="uq_tenant_profiles_tenant_version"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    industry: Mapped[str | None] = mapped_column(String(100), nullable=True)
    business_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    netsuite_account_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    chart_of_accounts: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    subsidiaries: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    item_types: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    custom_segments: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    fiscal_calendar: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    suiteql_naming: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped["Tenant"] = relationship("Tenant")
    confirmer: Mapped["User | None"] = relationship("User", foreign_keys=[confirmed_by])
