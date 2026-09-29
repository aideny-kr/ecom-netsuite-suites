"""Historical policy receipts, deliberately outside daily runs and case state."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TransactionPolicyReplay(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "transaction_policy_replays"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "evaluation_key"),
        ForeignKeyConstraint(
            ["tenant_id", "source_config_id"], ["transaction_ops_configs.tenant_id", "transaction_ops_configs.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "target_config_id"], ["transaction_ops_configs.tenant_id", "transaction_ops_configs.id"]
        ),
        CheckConstraint("status IN ('pending','finished','cancelled')", name="ck_policy_replay_status"),
    )
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenants.id"), index=True)
    evaluation_key: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    source_config_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    target_config_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    initiated_by: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("users.id"))
    request_json: Mapped[dict] = mapped_column(JSONB)
    source_snapshot: Mapped[dict] = mapped_column(JSONB)
    target_snapshot: Mapped[dict] = mapped_column(JSONB)
    manifest_json: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed: Mapped[int] = mapped_column(Integer, default=0)


class TransactionPolicyReplayEntry(Base):
    __tablename__ = "transaction_policy_replay_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "replay_id"], ["transaction_policy_replays.tenant_id", "transaction_policy_replays.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "order_reference"],
            [
                "transaction_ops_findings.tenant_id",
                "transaction_ops_findings.run_id",
                "transaction_ops_findings.order_reference",
            ],
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    replay_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    order_reference: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    finding_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    original_hash: Mapped[str] = mapped_column(String(64))
    result_json: Mapped[dict | None] = mapped_column(JSONB)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
