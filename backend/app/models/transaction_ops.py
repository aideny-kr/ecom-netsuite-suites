"""Durable transaction investigations, human decisions and pre-call operation ledger."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TransactionConfig(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "transaction_ops_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "config_key"),
        CheckConstraint("NOT schedule_enabled OR enabled", name="ck_tx_config_schedule"),
        CheckConstraint(
            "max_api_calls BETWEEN 1 AND 2000 AND max_orders BETWEEN 1 AND 10000", name="ck_tx_config_budget"
        ),
        CheckConstraint(
            "deadline_seconds BETWEEN 30 AND 3600 AND interval_minutes BETWEEN 5 AND 10080", name="ck_tx_config_clock"
        ),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)
    config_key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    source_step_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("celigo_flow_steps.id"))
    netsuite_connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("connections.id"))
    netsuite_account_id: Mapped[str] = mapped_column(String(255))
    subsidiary_id: Mapped[str] = mapped_column(String(255))
    record_type: Mapped[str] = mapped_column(String(50))
    target_step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("celigo_flow_steps.id"))
    mapping_json: Mapped[dict] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    interval_minutes: Mapped[int] = mapped_column(Integer)
    max_api_calls: Mapped[int] = mapped_column(Integer)
    max_orders: Mapped[int] = mapped_column(Integer)
    deadline_seconds: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class TransactionRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "transaction_ops_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "work_key"),
        ForeignKeyConstraint(
            ["tenant_id", "config_id"], ["transaction_ops_configs.tenant_id", "transaction_ops_configs.id"]
        ),
        CheckConstraint("status IN ('pending','running','finished')", name="ck_tx_run_status"),
        CheckConstraint("origin IN ('manual','chat','schedule','recovery')", name="ck_tx_run_origin"),
        CheckConstraint(
            "termination_reason IS NULL OR termination_reason IN ('done','budget','stall','error')",
            name="ck_tx_run_reason",
        ),
        CheckConstraint(
            "api_calls_used >= 0 AND api_calls_used <= max_api_calls "
            "AND orders_used >= 0 AND orders_used <= max_orders",
            name="ck_tx_run_spend",
        ),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)
    config_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    config: Mapped[TransactionConfig] = relationship(lazy="raise")
    work_key: Mapped[str] = mapped_column(String(64))
    origin: Mapped[str] = mapped_column(String(20))
    params_json: Mapped[dict] = mapped_column(JSONB)
    config_snapshot: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    termination_reason: Mapped[str | None] = mapped_column(String(20))
    max_api_calls: Mapped[int] = mapped_column(Integer)
    max_orders: Mapped[int] = mapped_column(Integer)
    api_calls_used: Mapped[int] = mapped_column(Integer, default=0)
    orders_used: Mapped[int] = mapped_column(Integer, default=0)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    initiated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TransactionProposal(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "transaction_ops_proposals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        Index(
            "uq_tx_proposal_active_work",
            "tenant_id",
            "work_key",
            unique=True,
            postgresql_where=text("status IN ('pending','approved')"),
        ),
        ForeignKeyConstraint(
            ["tenant_id", "config_id"], ["transaction_ops_configs.tenant_id", "transaction_ops_configs.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "run_id"], ["transaction_ops_runs.tenant_id", "transaction_ops_runs.id"]),
        CheckConstraint("status IN ('pending','approved','rejected','superseded')", name="ck_tx_proposal_status"),
        CheckConstraint(
            "action IN ('sync_missing_order','correct_amounts','resolve_celigo_error')", name="ck_tx_proposal_action"
        ),
        CheckConstraint(
            "status NOT IN ('approved','rejected') OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_tx_proposal_actor",
        ),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)
    config_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    work_key: Mapped[str] = mapped_column(String(64))
    source_record_id: Mapped[str] = mapped_column(String(255))
    order_reference: Mapped[str] = mapped_column(String(255))
    target_record_id: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(40))
    currency: Mapped[str] = mapped_column(String(3))
    netsuite_account_id: Mapped[str] = mapped_column(String(255))
    subsidiary_id: Mapped[str] = mapped_column(String(255))
    record_type: Mapped[str] = mapped_column(String(50))
    evidence_fingerprint: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    before_json: Mapped[dict] = mapped_column(JSONB)
    after_json: Mapped[dict] = mapped_column(JSONB)
    evidence_json: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(String(2000))


class TransactionOperation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "transaction_ops_operations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "work_key"),
        UniqueConstraint("tenant_id", "proposal_id"),
        Index(
            "uq_tx_operation_unsettled_entity",
            "tenant_id",
            "entity_key",
            unique=True,
            postgresql_where=text("status IN ('executing','unknown')"),
        ),
        ForeignKeyConstraint(
            ["tenant_id", "proposal_id"], ["transaction_ops_proposals.tenant_id", "transaction_ops_proposals.id"]
        ),
        CheckConstraint("status IN ('executing','verified','unknown','failed')", name="ck_tx_operation_status"),
        CheckConstraint(
            "max_api_calls BETWEEN 1 AND 96 AND api_calls_used >= 0 AND api_calls_used <= max_api_calls",
            name="ck_tx_operation_spend",
        ),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)
    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    work_key: Mapped[str] = mapped_column(String(64))
    entity_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="executing", index=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    max_api_calls: Mapped[int] = mapped_column(Integer)
    api_calls_used: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_json: Mapped[dict] = mapped_column(JSONB, default=dict)


class TransactionFinding(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "transaction_ops_findings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "run_id", "order_reference"),
        ForeignKeyConstraint(["tenant_id", "run_id"], ["transaction_ops_runs.tenant_id", "transaction_ops_runs.id"]),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    order_reference: Mapped[str] = mapped_column(String(100))
    report_json: Mapped[dict] = mapped_column(JSONB)
