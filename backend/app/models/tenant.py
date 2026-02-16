from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.connection import Connection
    from app.models.user import User


class Tenant(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    plan: Mapped[str] = mapped_column(String(50), default="trial", nullable=False)
    plan_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    config: Mapped["TenantConfig"] = relationship("TenantConfig", back_populates="tenant", uselist=False)
    users: Mapped[list["User"]] = relationship("User", back_populates="tenant")
    connections: Mapped[list["Connection"]] = relationship("Connection", back_populates="tenant")


class TenantConfig(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "tenant_configs"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), unique=True, nullable=False, index=True
    )
    subsidiaries: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    account_mappings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    posting_mode: Mapped[str] = mapped_column(String(50), default="lumpsum", nullable=False)
    posting_batch_size: Mapped[int] = mapped_column(default=100, nullable=False)
    posting_attach_evidence: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    netsuite_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="config")
