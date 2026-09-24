from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, event
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, ORMExecuteState, Session, mapped_column, relationship, with_loader_criteria

# Installs the session-flush guard that refuses Celigo-row writes coming from
# generic, provider-agnostic paths. Imported HERE, from the model itself, on
# purpose: a model importing a service is backwards, but it is the only
# placement under which a session for this model cannot be constructed without
# the listener loaded. Registering from an app entrypoint instead would leave
# workers, scripts, and the test harness silently unguarded the day someone
# adds a fifth way to build a Session. See app/services/celigo_write_guard.py.
import app.services.celigo_write_guard  # noqa: F401,E402  (import for side effect)
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

# Intentionally dead: nothing should refresh, health-check, or dispatch these.
# `superseded` is set by the NetSuite OAuth callback when a newer authorization takes
# over the tenant's single connection (2026-08-07). It is NOT `revoked` -- the row
# stays selectable so a later re-auth of that account reclaims it instead of creating
# a duplicate. Filter on THIS, not on `status != "revoked"`: the health audit used the
# latter, so it flipped superseded rows to `error` (their tokens are never refreshed,
# so they always eventually look expired), overwrote the "Superseded by ..." reason
# that is the only record of why the row was demoted, and thereby made them eligible
# for proactive refresh again -- against a connection deliberately retired.
RETIRED_CONNECTION_STATUSES = ("revoked", "superseded")


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


# ---------------------------------------------------------------------------
# The TypeSafe Jev row is invisible unless a query asks for it
# ---------------------------------------------------------------------------
# The Jev connection (provider "typesafe") holds a tenant's TypeSafe key and Jev mode, and
# is managed only by services/typesafe/access.py and its card (/connector-status/jev, which
# checks the key and serializes writes). Three review rounds on #314 each found one more
# generic route that could read or change it. So every ORM SELECT on Connection excludes it
# here, unless the statement carries ``.execution_options(include_jev_connection=True)``;
# a route that cannot load the row cannot change it. (Bulk ORM UPDATE/DELETE on this table
# is already refused by the Celigo write guard.) Refreshing an object already loaded (a
# column load) is left alone. Registered from the model module for the same reason as the
# Celigo write guard above: no session for this model can exist without it.
JEV_PROVIDER = "typesafe"
INCLUDE_JEV_CONNECTION = "include_jev_connection"


@event.listens_for(Session, "do_orm_execute")
def _hide_the_jev_connection(state: ORMExecuteState) -> None:
    if state.is_column_load or state.execution_options.get(INCLUDE_JEV_CONNECTION):
        return
    if state.is_select:
        state.statement = state.statement.options(
            with_loader_criteria(Connection, Connection.provider != JEV_PROVIDER, include_aliases=True)
        )
