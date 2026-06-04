import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

SYSTEM_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


class MetricDefinition(Base):
    __tablename__ = "metric_definitions"
    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_metric_tenant_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)  # currency|percent|ratio|count|days
    format: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)  # suiteql|bigquery|expression|cross_source
    blessed_spec: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    expression: Mapped[str | None] = mapped_column(Text, nullable=True)
    depends_on: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    params_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    dimensions: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    synonyms: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    intent_embedding = mapped_column(Vector(1536), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active", server_default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    provenance: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
