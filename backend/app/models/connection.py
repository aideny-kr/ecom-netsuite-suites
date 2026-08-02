from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.tenant import Tenant


# ---------------------------------------------------------------------------
# Shared status-allowlist constants -- single source of truth for values
# imported by more than one sync task/service module (mirrors
# TERMINAL_RESULT_STATUSES' home in four_bucket_classifier.py: Connection is a
# neutral module none of those consumers import each other through, so
# cross-imports can never form a cycle). Previously each site duplicated the
# literal ("active", "healthy") tuple independently.
# ---------------------------------------------------------------------------

# A connection in one of these statuses is fully healthy -- gates whether a
# sync's own service-layer lookup treats it as USABLE (e.g.
# get_netsuite_rest_connection, the stripe pre-flight guard,
# _count_active_stripe_connections).
ACTIVE_CONNECTION_STATUSES = ("active", "healthy")

# A connection in one of these statuses is DISPATCHED by the nightly/hourly
# fan-outs (netsuite_deposit_sync_all, stripe_sync_all) -- deliberately wider
# than ACTIVE_CONNECTION_STATUSES to include 'error': dispatching for an
# error-state connection lets the child task's guard/service-error path raise
# and record a failed job row every night the connection stays dead, instead
# of silently skipping it forever. (2026-07-29 incident: a NetSuite connection
# flipped to `error` and fell out of the active set -- the fan-out skipped it
# every night with no signal, and four days of mirror staleness were invisible
# in job history.) `revoked` and other intentionally-dead statuses stay
# excluded -- there's no path back to health for those, and dispatching them
# would just spam failures for a connection nobody intends to reactivate.
DISPATCHABLE_CONNECTION_STATUSES = ACTIVE_CONNECTION_STATUSES + ("error",)


class Connection(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "connections"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # shopify, stripe, netsuite
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)
    auth_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False)
    encryption_key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="connections")
