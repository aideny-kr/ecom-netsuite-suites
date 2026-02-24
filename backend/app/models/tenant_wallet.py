from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TenantWallet(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Credit ledger for tenant LLM consumption.

    Tracks included base credits (reset each billing period) and
    overage metered credits that get synced to Stripe hourly.
    """

    __tablename__ = "tenant_wallets"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), unique=True, nullable=False, index=True
    )

    # Stripe identifiers for syncing usage records
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_subscription_item_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Billing period tracking
    billing_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    billing_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Base credits (resets to plan limit at billing_period_start)
    base_credits_remaining: Mapped[int] = mapped_column(Integer, default=500, nullable=False)

    # Overage counter (incremented once base_credits_remaining == 0)
    metered_credits_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Watermark for Stripe sync delta calculation
    last_synced_metered_credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    tenant: Mapped["Tenant"] = relationship("Tenant", foreign_keys=[tenant_id])  # noqa: F821
