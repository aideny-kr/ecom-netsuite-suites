"""Immutable native observations for resumable reconciliation batch comparisons."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class TransactionEvidenceBatch(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "transaction_evidence_batches"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id"], ["transaction_ops_runs.tenant_id", "transaction_ops_runs.id"], ondelete="CASCADE"
        ),
        CheckConstraint("kind IN ('orders','refunds','dependencies')", name="ck_tx_evidence_batch_kind"),
        CheckConstraint("completed_at >= started_at", name="ck_tx_evidence_batch_times"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(16))
    context_hash: Mapped[str] = mapped_column(String(64))
    connection_fingerprint: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence_json: Mapped[dict] = mapped_column(JSONB)
