import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CanonicalMixin(TimestampMixin):
    """Common columns for all canonical tables."""

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    subsidiary_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Order(Base, UUIDPrimaryKeyMixin, CanonicalMixin):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_orders_dedupe"),
        Index("ix_orders_tenant_source_date", "tenant_id", "source_created_at"),
    )

    order_number: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(24, 6), nullable=False)
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(24, 6), nullable=True)
    tax_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 6), nullable=True)
    discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 6), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    source_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connections.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Payment(Base, UUIDPrimaryKeyMixin, CanonicalMixin):
    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("tenant_id", "dedupe_key", name="uq_payments_dedupe"),)

    order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    payment_method: Mapped[str | None] = mapped_column(String(100), nullable=True)


class Refund(Base, UUIDPrimaryKeyMixin, CanonicalMixin):
    __tablename__ = "refunds"
    __table_args__ = (UniqueConstraint("tenant_id", "dedupe_key", name="uq_refunds_dedupe"),)

    order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False)


class Payout(Base, UUIDPrimaryKeyMixin, CanonicalMixin):
    __tablename__ = "payouts"
    __table_args__ = (UniqueConstraint("tenant_id", "dedupe_key", name="uq_payouts_dedupe"),)

    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0, nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    arrival_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class PayoutLine(Base, UUIDPrimaryKeyMixin, CanonicalMixin):
    __tablename__ = "payout_lines"
    __table_args__ = (UniqueConstraint("tenant_id", "dedupe_key", name="uq_payout_lines_dedupe"),)

    payout_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("payouts.id"), nullable=True)
    line_type: Mapped[str] = mapped_column(String(100), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    fee: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0, nullable=False)
    net: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_order_id: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Dispute(Base, UUIDPrimaryKeyMixin, CanonicalMixin):
    __tablename__ = "disputes"
    __table_args__ = (UniqueConstraint("tenant_id", "dedupe_key", name="uq_disputes_dedupe"),)

    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_order_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    related_payment_id: Mapped[str | None] = mapped_column(String(255), nullable=True)


class NetsuitePosting(Base, UUIDPrimaryKeyMixin, CanonicalMixin):
    __tablename__ = "netsuite_postings"
    __table_args__ = (UniqueConstraint("tenant_id", "dedupe_key", name="uq_netsuite_postings_dedupe"),)

    netsuite_internal_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    record_type: Mapped[str] = mapped_column(String(100), nullable=False)
    transaction_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_payout_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Currency truth (Phase A, 2026-07-21): the TRANSACTION currency NetSuite
    # labeled the deposit with, plus the transaction-currency amount and rate.
    # ``currency``/``amount`` above keep meaning the SUBSIDIARY BASE currency/amount.
    transaction_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    foreign_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    exchange_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
