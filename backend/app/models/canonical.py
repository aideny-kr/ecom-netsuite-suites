import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    ColumnElement,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    and_,
    exists,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, aliased, mapped_column

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
        Index("ix_orders_source_reference", "tenant_id", "source_connection_id", "order_number"),
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


# Stripe balance-transaction types (PayoutLine.line_type) that move a customer's money.
# A card payment is a `charge`; a bank payment (ACH and similar, `py_…`) is a `payment`.
# Either can settle an order, and each has its own refund type. Matching selects payments
# through settled_customer_payment() and refunds through this tuple rather than naming a
# type, so no site can drop bank payments again.
CUSTOMER_PAYMENT_LINE_TYPES = ("charge", "payment")
CUSTOMER_REFUND_LINE_TYPES = ("refund", "payment_refund")
# Stripe records a bank payment before the bank confirms it. When the bank rejects it,
# Stripe reverses it with this type, whose source (related_order_id) is the payment's.
BANK_PAYMENT_FAILURE_LINE_TYPE = "payment_failure_refund"


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


def settled_customer_payment() -> ColumnElement[bool]:
    """SQL condition: the PayoutLine is a card charge or a bank payment whose money arrived.

    A bank payment the bank later rejected never settled its order, so it must neither
    match a deposit nor count as the order's payment. Stripe links the reversal to the
    payment only through their shared source (``related_order_id``); its description
    carries no order number. Card charges have no such reversal, because Stripe records
    a charge only once the money is captured.
    """
    failure = aliased(PayoutLine)
    return and_(
        PayoutLine.line_type.in_(CUSTOMER_PAYMENT_LINE_TYPES),
        ~exists().where(
            failure.tenant_id == PayoutLine.tenant_id,
            failure.line_type == BANK_PAYMENT_FAILURE_LINE_TYPE,
            failure.related_order_id == PayoutLine.related_order_id,
        ),
    )


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
