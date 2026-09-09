"""Side-effect log — one row per attempted external write, written BEFORE the call.

The row exists so that a crash between send and confirm leaves evidence. Without
it, a killed process leaves a write that either happened or did not, and nothing
records which — the state that created sandbox customer 5264348 on 2026-08-27
while the application reported the write as failed.

``.claude/rules/agent-graph.md`` #10 requires exactly this and records that it was
never built. This is that table.

Deliberately NOT a queue or a job table: nothing here drives execution. It is a
ledger of what we told NetSuite to do, written first so it survives us.
"""

import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class WriteSideEffect(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "write_side_effects"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )

    # The work-derived key, also written into the payload as externalId. NetSuite
    # enforces uniqueness on it, which is what makes a blind retry safe.
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)

    # Which account this was aimed at. A write is only reconcilable against the
    # connector it was sent to — the same value that decides sandbox vs
    # production, and the reason the card shows it so loudly.
    connector_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)

    record_type: Mapped[str] = mapped_column(String(80), nullable=False)
    mutation_type: Mapped[str] = mapped_column(String(20), nullable=False)  # create | update | upsert | delete

    # attempted | written | rejected | unknown — see SideEffectStatus.
    # Starts at 'attempted'. Nothing may set 'written' without a definite answer.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="attempted", index=True)

    # Set only once NetSuite has told us, or reconciliation has proven it.
    netsuite_record_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Batch provenance. NULL for a single-record write, which still gets a row —
    # phase 1 ships alone and must make the single-record timeout answerable.
    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    row_index: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Ties this write to the chat turn and audit trail that produced it.
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # The raw last answer, for forensics on a row a human has to resolve. The
    # ticket that motivated the indeterminate work (86bbhmxd1) asked for exactly
    # this: preserve what we could not read.
    last_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        # One row per key per tenant. Two attempts at the same work share a row
        # and update it — otherwise a retry would append a second row and the
        # log would imply two writes where there was one.
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_write_side_effect_tenant_key"),
        # The reconciliation query: "what is still in flight for this tenant?"
        Index("ix_write_side_effect_unsettled", "tenant_id", "status"),
    )
