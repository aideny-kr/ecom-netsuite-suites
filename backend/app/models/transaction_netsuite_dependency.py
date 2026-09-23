"""Provider record identities used by a saved reconciliation observation."""

import uuid

from sqlalchemy import ForeignKeyConstraint, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class TransactionNetSuiteDependency(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "transaction_netsuite_dependencies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "order_reference"],
            [
                "transaction_ops_findings.tenant_id",
                "transaction_ops_findings.run_id",
                "transaction_ops_findings.order_reference",
            ],
            ondelete="CASCADE",
            name="fk_tx_ns_dependency_finding",
        ),
        UniqueConstraint(
            "tenant_id", "run_id", "order_reference", "record_type", "record_id", name="uq_tx_ns_dependency"
        ),
        Index("ix_tx_ns_dependency_record", "tenant_id", "connection_id", "account_id", "record_type", "record_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    order_reference: Mapped[str] = mapped_column(String(100))
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    account_id: Mapped[str] = mapped_column(String(32))
    record_type: Mapped[str] = mapped_column(String(64))
    record_id: Mapped[str] = mapped_column(String(30))
