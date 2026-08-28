"""SQLAlchemy models for the Celigo flow-map tables.

Read `alembic/versions/094_celigo_flow_map.py` first -- its docstring is the
schema's source of truth, including every deliberate deviation from the task
brief (non-uniform unique keys, the two STORED GENERATED columns, the
CASCADE-vs-SET-NULL split between the five config-mirror tables and the two
audit tables). This module is a thin, column-for-column mirror of that
migration: no column is added, renamed, or loosened here that the migration
doesn't already have. If the schema needs to change, that's a new migration,
not model drift.

Task 5 of the Plan B flow-map plan: these models plus
`app/services/celigo/repository.py`'s idempotent upsert functions over the
seven tables below.

`branch_key` (CeligoFlowStep) and `dedup_key` (CeligoScript) are STORED
GENERATED columns in Postgres (`COALESCE(...)`, see the migration). They are
declared here with `sa.Computed(...)`, matching the migration's DDL exactly.
SQLAlchemy's INSERT/UPDATE compiler excludes a `Computed` column from the
generated column list automatically when no value is supplied for it -- so an
ORM object built from one of these classes can never accidentally try to
WRITE a value Postgres itself computes (and would reject). The repository
belt-and-suspenders this by simply never including either key in its own
`.values()` dicts; see that module's docstring for why both layers matter.

Composite indexes that don't map to a single mapped_column (`ix_celigo_
scripts_dedup_key`, `ix_celigo_script_attachments_script_celigo_id`,
`ix_celigo_flow_errors_tenant_trace_key`) are declared via `Index(...)` in
`__table_args__`, named to match the migration exactly -- `alembic env.py`
points `target_metadata` at `Base.metadata`, so a name or column-set mismatch
here would show up as a spurious ALTER TABLE the next time someone runs
`alembic revision --autogenerate`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

_EMPTY_JSONB = text("'{}'::jsonb")


class CeligoIntegration(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Mirrors one Celigo `GET /v1/integrations/{id}` object (sanitized)."""

    __tablename__ = "celigo_integrations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "celigo_connection_id", "celigo_id", name="uq_celigo_integrations_identity"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # CASCADE, NOT NULL -- a live mirror of Celigo config, meaningless without
    # its connection. See migration `_connection_fk()`.
    celigo_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    celigo_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    sandbox: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    mode: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    celigo_last_modified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_json: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_JSONB)


class CeligoFlow(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Mirrors one Celigo `GET /v1/flows/{id}` object (sanitized)."""

    __tablename__ = "celigo_flows"
    __table_args__ = (
        UniqueConstraint("tenant_id", "celigo_connection_id", "celigo_id", name="uq_celigo_flows_identity"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    celigo_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    integration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("celigo_integrations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    celigo_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    disabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    schedule: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    timezone: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Flows carry `_sourceId` too, not only scripts (observed-shapes.md finding
    # #1) -- stored, not yet acted on by any dedup logic here (migration
    # deviation 5's neighbor note; scripts are the one with dedup_key).
    source_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_description_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_description_detailed: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_description_generated_on: Mapped[str | None] = mapped_column(Text, nullable=True)
    celigo_last_modified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_json: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_JSONB)


class CeligoFlowStep(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One page-generator or page-processor step, extracted from a flow's
    top-level `pageGenerators`/`pageProcessors` OR from
    `routers[].branches[].pageProcessors` (observed-shapes.md: most steps in
    multi-subsidiary sales-order flows live in the latter, not the former --
    see `app/services/celigo/repository.py`'s `extract_flow_steps`).

    `celigo_id` here is the REFERENCED export/import id (`_exportId` /
    `_importId`), not an id this table owns the identity of -- the same
    export can be referenced from more than one flow or branch, which is why
    the unique key below is 5 columns wide, not the 3-tuple most of the other
    tables use.
    """

    __tablename__ = "celigo_flow_steps"
    __table_args__ = (
        CheckConstraint("role IN ('generator', 'processor')", name="ck_celigo_flow_steps_role"),
        UniqueConstraint(
            "tenant_id",
            "celigo_connection_id",
            "flow_id",
            "celigo_id",
            "branch_key",
            name="uq_celigo_flow_steps_identity",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    celigo_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    flow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("celigo_flows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    celigo_id: Mapped[str] = mapped_column(Text, nullable=False)  # referenced _exportId / _importId
    role: Mapped[str] = mapped_column(Text, nullable=False)  # 'generator' | 'processor'
    # Set only for steps sourced from routers[].branches[].pageProcessors.
    # NULL for top-level pageGenerators/pageProcessors.
    router_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    branch_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # STORED GENERATED (`COALESCE(branch_id, '$root')`) -- see module
    # docstring. Never set explicitly; the repository never includes this key
    # in an INSERT/UPDATE values dict, and this Computed() declaration makes
    # SQLAlchemy itself exclude it too if a caller ever forgot.
    branch_key: Mapped[str] = mapped_column(
        Text, Computed("COALESCE(branch_id, '$root')", persisted=True), nullable=False
    )
    # Ordinal position within its containing array (top-level or one branch)
    # -- the unique-key columns above don't preserve original step order.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Live on the REFERENCED export/import object, not the pageProcessor
    # entry itself -- null unless a follow-up fetch (no such fetcher exists
    # yet) populates them. Never invented.
    adaptor_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    connection_celigo_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # filter/responseMapping ARE present directly on the pageProcessor entry
    # (sanitizer.py's _PAGE_PROCESSOR) -- no extra fetch needed for these two.
    filter_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    mapping_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # responseMapping
    proceed_on_failure: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # processor-only
    skip_retries: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # generator-only
    raw_json: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=_EMPTY_JSONB)


class CeligoScript(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Mirrors one Celigo script (list or single-get projection, sanitized).

    `source_id`/`dedup_key` implement the clone-lineage dedup
    (observed-shapes.md, live-confirmed): a clone carries `_sourceId`
    pointing at the original; the original itself has NO `_sourceId`.
    `dedup_key = COALESCE(source_id, celigo_id)` gives every original its own
    key and every clone the same key as its original -- see
    `app/services/celigo/repository.py`'s `list_logical_scripts` for the
    read-side grouping this enables.
    """

    __tablename__ = "celigo_scripts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "celigo_connection_id", "celigo_id", name="uq_celigo_scripts_identity"),
        Index(
            "ix_celigo_scripts_dedup_key",
            "tenant_id",
            "celigo_connection_id",
            "dedup_key",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    celigo_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    celigo_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)  # absent from list-mode fetches
    # Repository-computed (sha256 of `content`), not Celigo-sourced -- see
    # repository.py's `_content_hash`. Nullable because `content` itself is.
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # NULL on the clone original
    # STORED GENERATED -- see class docstring and module docstring. Never set
    # explicitly.
    dedup_key: Mapped[str] = mapped_column(
        Text, Computed("COALESCE(source_id, celigo_id)", persisted=True), nullable=False
    )
    sandbox: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    celigo_last_modified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CeligoScriptAttachment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One `graph.walk_script_refs` occurrence (a `ScriptRef`) -- not a
    Celigo-native object, so it has no Celigo id of its own. Its identity is
    `(tenant_id, flow_id, json_path)`: `json_path` is computed by
    `walk_script_refs` relative to the flow object root, so it is unique per
    occurrence within one flow by construction. `json_path` is IDENTITY, not
    decoration -- see `app/services/celigo/repository.py`'s module docstring.

    Has BOTH `flow_id` (NOT NULL) and `flow_step_id` (nullable): a
    `routers[].script` ref belongs to the router, not to any one step, so
    there is no `celigo_flow_steps` row to point it at -- `flow_id` anchors
    every attachment regardless of where in the flow it was found;
    `flow_step_id` is populated only when the ref falls inside a specific
    step's subtree.
    """

    __tablename__ = "celigo_script_attachments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "flow_id", "json_path", name="uq_celigo_script_attachments_identity"),
        Index(
            "ix_celigo_script_attachments_script_celigo_id",
            "tenant_id",
            "celigo_connection_id",
            "script_celigo_id",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    celigo_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    flow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("celigo_flows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    flow_step_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("celigo_flow_steps.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Nullable FK + a NOT NULL raw string: script sync can lag flow sync, and
    # the raw _scriptId the walker found must survive being stored even
    # before the corresponding celigo_scripts row exists locally.
    script_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("celigo_scripts.id", ondelete="SET NULL"), nullable=True
    )
    script_celigo_id: Mapped[str] = mapped_column(Text, nullable=False)  # raw _scriptId, always available
    function_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    json_path: Mapped[str] = mapped_column(Text, nullable=False)  # relative to the flow object root -- IDENTITY
    site_type: Mapped[str | None] = mapped_column(Text, nullable=True)  # hook | filter | transform | router | unknown


class CeligoErrorSignature(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A locally computed error fingerprint, not a raw Celigo object -- key is
    `(tenant_id, celigo_connection_id, fingerprint)`.

    `celigo_connection_id` is SET NULL, nullable -- this table (with
    `celigo_flow_errors`) is the audit trail (design spec G2: "outlives the
    source") and must survive a `connections` row being deleted, unlike the
    five config-mirror tables above.
    """

    __tablename__ = "celigo_error_signatures"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "celigo_connection_id", "fingerprint", name="uq_celigo_error_signatures_identity"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    celigo_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connections.id", ondelete="SET NULL"), nullable=True, index=True
    )
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    code: Mapped[str | None] = mapped_column(Text, nullable=True)
    # PII-bearing (design spec §4.3) -- never logged, same posture as
    # CeligoFlowError.message below.
    sample_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CeligoFlowError(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """THE audit trail (design spec G2). Surviving Celigo's ~30-day purge is
    the entire point of this table -- NEVER DELETE A ROW HERE. See
    `app/services/celigo/repository.py`'s module docstring: there is no
    delete-capable function for this table anywhere in that module, on
    purpose. `resolved_at`/`purged_at` are the only state transitions; both
    are distinct from Celigo's own `purge_at` deadline (echoed from the API).

    `celigo_connection_id`/`flow_id`/`flow_step_id`/`signature_id` are ALL
    SET NULL on delete, never CASCADE: this table must outlive every one of
    its parents, the same way it must outlive Celigo's own purge.
    """

    __tablename__ = "celigo_flow_errors"
    __table_args__ = (
        UniqueConstraint("tenant_id", "celigo_connection_id", "celigo_id", name="uq_celigo_flow_errors_identity"),
        Index("ix_celigo_flow_errors_tenant_trace_key", "tenant_id", "trace_key"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    celigo_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connections.id", ondelete="SET NULL"), nullable=True, index=True
    )
    flow_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("celigo_flows.id", ondelete="SET NULL"), nullable=True, index=True
    )
    flow_step_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("celigo_flow_steps.id", ondelete="SET NULL"), nullable=True, index=True
    )
    signature_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("celigo_error_signatures.id", ondelete="SET NULL"), nullable=True, index=True
    )
    celigo_id: Mapped[str] = mapped_column(Text, nullable=False)  # errorId
    trace_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_data_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    code: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Verbatim, deliberately NOT stripped (design spec: "the message IS the
    # diagnosis") -- PII-bearing, never logged.
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    purge_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # Celigo's own deadline
    flow_job_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stored, never branched on (global constraint) -- every observed error
    # is retriable=true and none would succeed on retry.
    retriable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # WE noticed it vanished
