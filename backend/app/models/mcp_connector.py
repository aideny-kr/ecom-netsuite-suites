from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.tenant import Tenant


class McpConnector(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "mcp_connectors"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # netsuite_mcp, shopify_mcp, custom
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    server_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(20), default="none", nullable=False)  # bearer, api_key, none
    encrypted_credentials: Mapped[str | None] = mapped_column(Text, nullable=True)
    encryption_key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)
    discovered_tools: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="mcp_connectors")
