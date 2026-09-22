"""Latest sanitized Solidus detail observation, shared by bounded read-only scans."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TransactionSourceSnapshot(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "transaction_source_snapshots"
    __table_args__ = (UniqueConstraint("tenant_id", "connection_id", "order_reference", name="uq_tx_source_snapshot"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connections.id", ondelete="CASCADE")
    )
    order_reference: Mapped[str] = mapped_column(String(100))
    connection_fingerprint: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence_json: Mapped[dict] = mapped_column(JSONB)
