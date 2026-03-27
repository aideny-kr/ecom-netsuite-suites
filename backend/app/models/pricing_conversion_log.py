"""Pricing conversion audit log."""
import uuid
from sqlalchemy import ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, UUIDPrimaryKeyMixin, TimestampMixin


class PricingConversionLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "pricing_conversion_logs"
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    input_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    output_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    sku_count: Mapped[int] = mapped_column(Integer, nullable=False)
    currency_count: Mapped[int] = mapped_column(Integer, nullable=False)
    config_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
