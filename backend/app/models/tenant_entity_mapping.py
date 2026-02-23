from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TenantEntityMapping(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """High-speed fuzzy entity lookup via pg_trgm.

    Maps natural-language entity names (e.g. "Inventory Processor") to their
    NetSuite script IDs (e.g. "customrecord_r_inv_processor") per tenant.

    Indexes (created in migration 025 via raw SQL):
    - Composite GIN: (tenant_id, natural_name gin_trgm_ops) for sub-100ms fuzzy matching
    - Unique: (tenant_id, entity_type, script_id) for upsert deduplication
    """

    __tablename__ = "tenant_entity_mapping"

    __table_args__ = (
        UniqueConstraint("tenant_id", "entity_type", "script_id", name="uq_tenant_entity_type_script"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    natural_name: Mapped[str] = mapped_column(String(255), nullable=False)
    script_id: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
