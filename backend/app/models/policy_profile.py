from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.tenant import Tenant
    from app.models.user import User


class PolicyProfile(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "policy_profiles"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    read_only_mode: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    allowed_record_types: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    blocked_fields: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    max_rows_per_query: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)
    require_row_limit: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    custom_rules: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    tenant: Mapped["Tenant"] = relationship("Tenant")
    creator: Mapped["User | None"] = relationship("User", foreign_keys=[created_by])
