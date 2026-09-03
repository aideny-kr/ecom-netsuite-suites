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
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ColumnElement,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    and_,
    func,
    or_,
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
    # Whatever JSON Celigo sent, mirrored. The only shape seen live is a cron
    # STRING ("? 0 */6 * * *"); the object form `dict | None` used to claim
    # here was a fixture's, and the API models built on it 500d on the string
    # (2026-09-01). `Any` on purpose, matching the API's `CeligoSchedule =
    # JsonValue`: this column does not vouch for the shape, so its type must
    # not pretend to either.
    schedule: Mapped[Any] = mapped_column(JSONB, nullable=True)
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
    # entry itself -- null until `sync_service.py`'s Phase D (Task 7 fix
    # round 1) backfills them from that object's own fetch. Never invented.
    adaptor_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    connection_celigo_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Task 11's provenance input (Task 7 fix round 2, migration 096) -- also
    # live on the referenced export/import object, backfilled by the same
    # Phase D pass. `record_type` is shared: `netsuite_da.recordType`
    # (imports) and `netsuite.restlet.recordType` (exports) are the same
    # semantic field under different parent keys. `operation` is import-only
    # (`netsuite_da.operation`); `search_id` is export-only (`netsuite.
    # restlet.searchId`) -- each stays NULL for the kind that doesn't carry
    # it. See migration 096's own docstring for the raw_json-vs-dedicated-
    # columns design call.
    record_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    operation: Mapped[str | None] = mapped_column(Text, nullable=True)
    search_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The export/import's own `name` as typed in Celigo ("Get New Sales Orders",
    "Update Currency"). Lives on the REFERENCED object, so sync Phase D backfills
    it onto every step row that references that celigo_id (migration 097). NULL
    until the first post-097 sync; the UI then shows an honest fallback, never an
    invented name."""
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
    `(tenant_id, flow_id, json_path)`, and `json_path` is IDENTITY, not
    decoration.

    `json_path` is unique within one flow BY CONSTRUCTION, but that is not
    automatic and was originally wrong: `walk_script_refs` computes a path
    relative to the object it walked, and a flow only REFERENCES its
    exports/imports by id, so two of them in one flow each carrying
    `transform.script` produced identical paths and silently overwrote each
    other's row (whole-branch review finding 1). A path coming off an
    export/import is therefore stored qualified with that object's celigo id
    (`imp_1.transform.script`); a path coming off the flow object itself is
    already flow-relative and stored as-is (`routers[0].script`).
    `app/services/celigo/repository.py`'s `qualify_json_path` owns that rule
    and is the only place allowed to apply it.

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
    # IDENTITY. Flow-relative, qualified with the owning export/import's
    # celigo id when the ref was walked off one -- see the class docstring.
    json_path: Mapped[str] = mapped_column(Text, nullable=False)
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


def celigo_error_is_open() -> ColumnElement[bool]:
    """The single, canonical definition of an OPEN `celigo_flow_errors` row:
    not resolved AND not purged.

    WHOLE-BRANCH REVIEW FINDING 5 (2026-08-27): `app/api/v1/celigo_flows.py`'s
    flow-list open-count query and `app/services/celigo/errors.py`'s
    `occurrence_count` aggregate used to compute "open" two different ways --
    the former filtered `resolved_at IS NULL AND purged_at IS NULL`, the
    latter only `resolved_at IS NULL`. `sync_service._purge_expired_errors`
    sets `purged_at` independently of `resolved_at` (an error can be purged
    by Celigo's own ~30-day window without this app ever having seen it
    resolve), so a purged-but-unresolved row was counted by one and not the
    other -- the flow card and the signature panel showed different totals
    for the SAME errors, both presented as authoritative.

    "Not purged" is part of "open" because `purged_at` means Celigo itself
    has already destroyed the underlying record (`repository.
    mark_flow_errors_purged`'s own docstring) -- there is nothing left to
    action, the same as a resolved error, even though this app never saw it
    resolve.

    WHAT SINGLE-SOURCING HERE DOES AND DOES NOT BUY (corrected, re-review R3,
    2026-08-27 -- the previous wording claimed "a third caller added later has
    no way to invent a third definition -- it has to import this one", and the
    counter-example was already in one of the two importing modules).
    Nothing forces the import. Any query can write `resolved_at IS NULL` again,
    and `errors.upsert_errors` DID: its pre-call snapshot used that alone, so a
    purged row counted as previously open and got stamped with a `resolved_at`
    this app never observed. What this function buys is that the definition
    exists in one place and a diverging query is VISIBLE as a hand-rolled
    predicate next to an available import -- not that divergence is
    impossible. Today's known callers, all executed: `celigo_flows.py`'s
    open-count query, `errors.py`'s `occurrence_count` aggregate, and
    `errors.py`'s pre-call snapshot. `resolved_at IS NULL` on its own is a
    legitimate predicate for "this row is not already finished with" (as that
    snapshot's WHERE clause now uses it); it is only wrong when it is used to
    mean OPEN."""
    return and_(CeligoFlowError.resolved_at.is_(None), CeligoFlowError.purged_at.is_(None))


def celigo_flow_is_on_demand() -> ColumnElement[bool]:
    """A flow with no schedule runs on demand. Celigo sends the schedule as a
    cron STRING, JSON null, or "" -- all three mean on demand; SQL NULL too.

    `CeligoFlow.schedule[()].astext` -- not the bare `.astext` the task brief's
    prose sketched -- compiles to `schedule #>> '{}'` (whole-value-as-text).
    SQLAlchemy 2.0's JSON/JSONB comparator only defines `.astext` on the
    result of an INDEX operation (`col[key].astext`, `->>`); calling it on the
    column itself raises `AttributeError` (verified directly against this
    version, both in bare SQLAlchemy Core and against this table). Indexing
    with an empty path tuple is the documented way to reach the root-value
    form of the same operator."""
    typeof = func.jsonb_typeof(CeligoFlow.schedule)
    return or_(
        CeligoFlow.schedule.is_(None),
        typeof == "null",
        and_(typeof == "string", CeligoFlow.schedule[()].astext == ""),
    )


def celigo_integration_is_production() -> ColumnElement[bool]:
    """The single definition of a PRODUCTION `celigo_integrations` row:
    `sandbox IS NOT TRUE`.

    The flow map is production only (operator directive 2026-09-01: "don't
    bring sandbox celigo, just production"). `IS NOT TRUE`, not `= false`: a
    NULL flag means Celigo omitted it, and hiding on an absent field would let
    a sanitizer or API change silently erase real integrations.

    Every read of these tables in `app/api/v1/celigo_flows.py` applies this
    -- the list, the per-integration flows, the flow detail (through the
    flow's integration) and the script sites. PR #216's first cut put the
    filter in ONE endpoint's WHERE clause; the gate found the other routes
    still served a sandbox row by id. Same lesson as `celigo_error_is_open`
    above: nothing forces a future query to import this, but a named
    predicate is what a reviewer greps for.

    The sync's write-side twin is `sync_service._is_sandbox` (`obj.get(
    "sandbox") is True` on the raw Celigo object) -- the same rule, stated
    for a dict rather than a column."""
    return CeligoIntegration.sandbox.isnot(True)


def celigo_script_is_production() -> ColumnElement[bool]:
    """`celigo_scripts` twin of `celigo_integration_is_production`. Scripts
    carry their own `sandbox` flag (sanitizer `_SCRIPT` allowlist); on the
    live account 132 of 259 were sandbox copies, and a clone-family count
    that summed both environments was wrong by about half."""
    return CeligoScript.sandbox.isnot(True)


class CeligoConfigChange(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Task 7 (migration 095): one field-level drift event, appended by
    `app.services.celigo.sync_service` when a resynced object's watched field
    differs from what was previously stored -- design spec §4.5's `disabled`,
    `schedule`, `mapping_json`, `filter_json`, `content_hash`. Append-only,
    like `CeligoFlowError`: a row here is a historical fact ("field X changed
    from A to B at time T"), never updated or deleted by anything in this
    branch.

    Polymorphic over three object kinds (`object_kind`, DB-CHECK'd to 'flow' |
    'flow_step' | 'script' in the migration) rather than three separate
    tables, because the watched set spans three different config-mirror
    tables (`celigo_flows.disabled`/`schedule`, `celigo_flow_steps.
    mapping_json`/`filter_json`, `celigo_scripts.content_hash`) and a future
    drift-history feed wants one table, not three to union. `object_id` has
    NO foreign key -- a single column can't reference three different tables,
    and the parent config-mirror rows are already CASCADE-deleted with their
    connection (see migration 095's docstring), so an orphaned `object_id`
    here is inert, the same way `celigo_script_attachments.script_celigo_id`
    already tolerates a missing `celigo_scripts` row by design. `celigo_id`
    (the drifted object's raw Celigo id) is stored alongside it for exactly
    that reason: it survives even if `object_id`'s row is later gone.

    `flow_id` (nullable, REAL FK to `celigo_flows`, CASCADE) is populated for
    'flow' and 'flow_step' kinds -- both always belong to exactly one flow --
    and left NULL for 'script' kind, since one script can be attached from
    many flows or none.

    `old_value`/`new_value` are JSONB rather than typed per field: the
    watched set spans four different Python types across its five fields
    (`disabled` is bool; `schedule`/`mapping_json`/`filter_json` are dicts;
    `content_hash` is a string) and JSONB stores any of them without a lossy
    string cast. Both being NULL together is a legitimate row (e.g. a
    `schedule` dict that became explicitly null).
    """

    __tablename__ = "celigo_config_changes"
    __table_args__ = (Index("ix_celigo_config_changes_tenant_object", "tenant_id", "object_kind", "object_id"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    celigo_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    flow_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("celigo_flows.id", ondelete="CASCADE"), nullable=True, index=True
    )
    object_kind: Mapped[str] = mapped_column(Text, nullable=False)  # 'flow' | 'flow_step' | 'script'
    object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)  # polymorphic, no FK
    celigo_id: Mapped[str] = mapped_column(Text, nullable=False)  # raw Celigo id of the object that drifted
    field: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[object | None] = mapped_column(JSONB, nullable=True)
