"""Task 1 (extraction) -- the read/aggregation queries behind `celigo_flows.py`'s
route handlers, moved here so the routes and the (future) chat tools
(`docs/superpowers/specs/2026-09-04-celigo-chat-access.md` §3-§4) call ONE
query definition instead of two drifting copies. Every function below is a
straight extraction of an existing route's query logic -- same predicates,
same tenant filters, same inline rationale (moved, not paraphrased) -- with
no behavior change. `celigo_flows.py`'s routes are now adapters: resolve the
connection/404 where that was the route's job, call the function here, map
the returned dataclass onto the route's own Out model.

PLAIN DATACLASSES, DELIBERATELY -- not Pydantic Out models (this module has
no FastAPI response-model concern; a chat tool consuming these dataclasses
must not need `app.api.v1.celigo_flows` importable) and not ORM rows (a
caller must never hold a live `AsyncSession`-bound object past this
function's `return`).

N2 SHAPE RULE (spec §1: "Script bodies stay out ... enforced by shape, not
by a guarded parameter"): no function below SELECTS `CeligoScript.content`
or `content_hash` as a projected column -- `flow_summaries`' divergence
check FILTERS on `content_hash` (a `WHERE`/`HAVING` predicate never returns
the column's value), and `flow_detail`'s script-family query fetches whole
`CeligoScript` ORM rows only to hand them to `topology.script_family_facts`,
which derives `size_chars`/`content_diverged` from the content -- it never
surfaces `content` itself into the dataclasses this module returns. Pinned
by `tests/api/test_celigo_read_queries_parity.py::TestNoScriptContentSelected`.

EXPLICIT TENANT SCOPING ON EVERY QUERY -- see `celigo_flows.py`'s module
docstring for the RLS-is-the-backstop-not-the-plan rationale; unchanged by
this move.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import JsonValue
from sqlalchemy import and_, case, distinct, func, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.celigo import (
    CeligoConfigChange,
    CeligoErrorSignature,
    CeligoFlow,
    CeligoFlowError,
    CeligoFlowStep,
    CeligoIntegration,
    CeligoScript,
    CeligoScriptAttachment,
    celigo_error_is_open,
    celigo_flow_is_on_demand,
    celigo_integration_is_production,
    celigo_script_is_production,
)
from app.models.connection import Connection
from app.models.pipeline import CursorState
from app.services.celigo.repository import parse_celigo_timestamp
from app.services.celigo.topology import (
    ScriptFamilyFact,
    adaptor_family,
    project_routers,
    script_family_facts,
    step_kind,
)


async def _get_celigo_connection(db: AsyncSession, tenant_id) -> Connection | None:
    """Moved from `app/api/v1/connector_status.py` (task 1 brief) -- the
    tenant's active (non-revoked) Celigo flow-map connection, if any. Every
    read in this module that needs to scope by connection resolves it
    through this one lookup, never a bespoke query."""
    result = await db.execute(
        select(Connection).where(
            Connection.tenant_id == tenant_id,
            Connection.provider == "celigo",
            Connection.status != "revoked",
        )
    )
    return result.scalar_one_or_none()


def _join_production_integration(stmt, tenant_id):
    """Moved from `app/api/v1/celigo_flows.py` (task 1 brief). INNER-join
    `CeligoIntegration` onto a statement whose FROM already carries
    `CeligoFlow`, with the tenant predicate AND
    `celigo_integration_is_production()` on the ON clause -- so the join is
    the production filter, not a WHERE someone has to remember. Used by
    `flow_detail` and (still in `celigo_flows.py`) the script sites query;
    one place to get it right."""
    return stmt.join(
        CeligoIntegration,
        and_(
            CeligoIntegration.id == CeligoFlow.integration_id,
            CeligoIntegration.tenant_id == tenant_id,
            celigo_integration_is_production(),
        ),
    )


# ---------------------------------------------------------------------------
# Dataclasses -- see module docstring for why these are plain dataclasses,
# not Pydantic Out models. Field names mirror the Out models in
# `celigo_flows.py` 1:1 so the route's mapping is a mechanical
# `Out(**dataclasses.asdict(x))`; see each Out model's own docstring there
# for the field-level rationale (not repeated here, to avoid the two
# copies of the same fact that field-level docstrings on both sides would
# become).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordWrite:
    record_type: str
    count: int


@dataclass(frozen=True)
class FlowSchedule:
    id: str
    name: str
    disabled: bool | None
    schedule: JsonValue
    last_executed_at: datetime | None


@dataclass(frozen=True)
class IntegrationSummary:
    id: str
    celigo_id: str
    name: str
    sandbox: bool | None
    mode: str | None
    description: str | None
    celigo_last_modified: datetime | None
    flow_count: int
    scheduled_count: int
    on_demand_count: int
    paused_count: int
    step_count: int
    router_count: int
    lookup_count: int
    script_count: int
    no_run_count: int
    error_count: int
    signature_count: int
    changes_last_24h: int
    last_run_at: datetime | None
    writes: list[RecordWrite] = field(default_factory=list)
    adaptor_families: list[str] = field(default_factory=list)
    flow_schedules: list[FlowSchedule] = field(default_factory=list)
    errors_checked_at: datetime | None = None


@dataclass(frozen=True)
class SyncStatus:
    last_synced_at: datetime | None


@dataclass(frozen=True)
class FlowSummary:
    id: str
    celigo_id: str
    name: str
    disabled: bool | None
    schedule: JsonValue
    timezone: str | None
    last_executed_at: datetime | None
    error_count: int
    signature_count: int
    step_count: int
    router_count: int
    branch_count: int
    lookup_count: int
    script_count: int
    diverged_family_count: int
    celigo_last_modified: datetime | None
    errors_checked_at: datetime | None
    writes: list[RecordWrite] = field(default_factory=list)


@dataclass(frozen=True)
class Attachment:
    id: str
    flow_id: str
    flow_step_id: str | None
    script_id: str | None
    script_celigo_id: str
    function_name: str | None
    json_path: str
    site_type: str | None
    script_name: str | None = None
    script_size_chars: int | None = None
    script_copies_count: int | None = None
    script_versions_count: int | None = None
    script_version_letter: str | None = None
    script_content_diverged: bool | None = None


@dataclass(frozen=True)
class FlowStep:
    id: str
    celigo_id: str
    role: str
    router_id: str | None
    branch_id: str | None
    branch_key: str
    sequence: int
    adaptor_type: str | None
    connection_celigo_id: str | None
    reference_name: str | None
    filter_json: JsonValue
    mapping_json: JsonValue
    proceed_on_failure: bool | None
    skip_retries: bool | None
    kind: str
    record_type: str | None
    operation: str | None
    search_id: str | None
    error_count: int
    attachments: list[Attachment] = field(default_factory=list)


@dataclass(frozen=True)
class FlowDetail:
    id: str
    integration_id: str
    celigo_id: str
    name: str
    disabled: bool | None
    schedule: JsonValue
    timezone: str | None
    last_executed_at: datetime | None
    source_id: str | None
    ai_description_summary: str | None
    ai_description_detailed: str | None
    celigo_last_modified: datetime | None
    celigo_open_error_count: int | None
    last_error_at: datetime | None
    error_count: int
    signature_count: int
    errors_checked_at: datetime | None
    steps: list[FlowStep] = field(default_factory=list)
    unassigned_attachments: list[Attachment] = field(default_factory=list)
    # `topology.project_routers`'s own return shape (list of plain dicts) --
    # relayed as-is, same as `get_flow_detail` always did before wrapping
    # each dict in `CeligoRouterOut(**r)`.
    routers: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class ErrorSignature:
    id: str
    fingerprint: str
    source: str | None
    code: str | None
    sample_message: str | None
    occurrence_count: int
    first_seen: datetime | None
    last_seen: datetime | None


@dataclass(frozen=True)
class FlowError:
    id: str
    celigo_id: str
    flow_id: str | None
    flow_step_id: str | None
    trace_key: str | None
    source: str | None
    code: str | None
    message: str | None
    occurred_at: datetime | None
    purge_at: datetime | None
    resolved_at: datetime | None
    purged_at: datetime | None
    retriable: bool | None


@dataclass(frozen=True)
class FlowErrorGroup:
    signature: ErrorSignature | None
    count: int
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    retriable: bool | None
    purge_at: datetime | None
    step_ids: list[str | None] = field(default_factory=list)
    trace_keys: list[str] = field(default_factory=list)
    errors: list[FlowError] = field(default_factory=list)


@dataclass(frozen=True)
class ErrorGroups:
    flow_id: str
    status: Literal["open", "resolved"]
    total: int
    groups: list[FlowErrorGroup] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Resolver return shapes -- task 3 (chat tools, spec §3). A chat argument is
# free text ("integration": id or name fragment; "flow": id or exact name),
# never a validated path param the way the routes' `uuid.UUID` FastAPI
# parameter is -- these two dataclasses are deliberately NOT the same shape
# as `IntegrationSummary`/`FlowSummary` above: a resolver's only job is
# "which row(s), if any, did the caller mean", not the full aggregate a tool
# then queries for separately via the functions above.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntegrationSummaryRef:
    id: str
    name: str


@dataclass(frozen=True)
class FlowRef:
    id: str
    name: str
    integration_id: str
    integration_name: str
    # Carried here (not re-queried by the caller) because the tool-layer
    # "no open errors for a CHECKED flow" honesty line (spec §8) needs it
    # for the single-flow case, and this resolver already holds a live
    # `CeligoFlow` row via the same production join that would otherwise be
    # re-executed just to read one column back off it.
    errors_checked_at: datetime | None


def _error_out(e: CeligoFlowError) -> FlowError:
    return FlowError(
        id=str(e.id),
        celigo_id=e.celigo_id,
        flow_id=str(e.flow_id) if e.flow_id else None,
        flow_step_id=str(e.flow_step_id) if e.flow_step_id else None,
        trace_key=e.trace_key,
        source=e.source,
        code=e.code,
        message=e.message,
        occurred_at=e.occurred_at,
        purge_at=e.purge_at,
        resolved_at=e.resolved_at,
        purged_at=e.purged_at,
        retriable=e.retriable,
    )


def _signature_out(sig: CeligoErrorSignature) -> ErrorSignature:
    return ErrorSignature(
        id=str(sig.id),
        fingerprint=sig.fingerprint,
        source=sig.source,
        code=sig.code,
        sample_message=sig.sample_message,
        occurrence_count=sig.occurrence_count,
        first_seen=sig.first_seen,
        last_seen=sig.last_seen,
    )


# ---------------------------------------------------------------------------
# integration_summaries -- moved from `list_integrations`
# ---------------------------------------------------------------------------


async def integration_summaries(db: AsyncSession, *, tenant_id: uuid.UUID) -> list[IntegrationSummary]:
    """PRODUCTION integrations synced under the tenant's currently active
    Celigo connection. Empty (never 404) when there is no active connection
    -- that is a legitimate "not connected yet" state, not an error.

    Sandbox integrations are excluded (operator directive 2026-09-01: "don't
    bring sandbox celigo, just production") through the ONE shared predicate,
    `celigo_integration_is_production()` -- see its docstring for the NULL
    rule and for why every other read in this module applies it too."""
    connection = await _get_celigo_connection(db, tenant_id)
    if connection is None:
        return []

    integrations = (
        (
            await db.execute(
                select(CeligoIntegration)
                .where(
                    CeligoIntegration.tenant_id == tenant_id,
                    CeligoIntegration.celigo_connection_id == connection.id,
                    celigo_integration_is_production(),
                )
                .order_by(CeligoIntegration.name)
            )
        )
        .scalars()
        .all()
    )
    if not integrations:
        return []

    integration_ids = [i.id for i in integrations]

    # Flow-schedule buckets: ONE grouped query, every bucket its own SQL
    # filter (not derived by subtraction) so `scheduled_count + on_demand_count
    # + paused_count == flow_count` holds by construction -- a flow can never
    # land in more than one bucket's filter, or in none. `disabled IS TRUE`
    # wins first regardless of schedule shape (a paused flow's schedule is
    # irrelevant to the operator); `celigo_flow_is_on_demand()` (models.py)
    # decides on-demand vs scheduled only among the rest.
    on_demand_pred = celigo_flow_is_on_demand()
    not_paused = CeligoFlow.disabled.isnot(True)
    bucket_rows = (
        await db.execute(
            select(
                CeligoFlow.integration_id,
                func.count().label("flow_count"),
                func.count().filter(CeligoFlow.disabled.is_(True)).label("paused_count"),
                func.count().filter(and_(not_paused, on_demand_pred)).label("on_demand_count"),
                func.count().filter(and_(not_paused, ~on_demand_pred)).label("scheduled_count"),
                func.count().filter(CeligoFlow.last_executed_at.is_(None)).label("no_run_count"),
                func.max(CeligoFlow.last_executed_at).label("last_run_at"),
            )
            .where(CeligoFlow.tenant_id == tenant_id, CeligoFlow.integration_id.in_(integration_ids))
            .group_by(CeligoFlow.integration_id)
        )
    ).all()
    buckets_by_integration = {row.integration_id: row for row in bucket_rows}

    # Per-flow schedule list -- a second, PLAIN (non-aggregated) projection
    # for the drill-down list; the buckets above never touch this query.
    schedule_rows = (
        await db.execute(
            select(
                CeligoFlow.integration_id,
                CeligoFlow.id,
                CeligoFlow.name,
                CeligoFlow.disabled,
                CeligoFlow.schedule,
                CeligoFlow.last_executed_at,
            )
            .where(CeligoFlow.tenant_id == tenant_id, CeligoFlow.integration_id.in_(integration_ids))
            .order_by(CeligoFlow.name)
        )
    ).all()
    schedules_by_integration: dict[uuid.UUID, list[FlowSchedule]] = defaultdict(list)
    for row in schedule_rows:
        schedules_by_integration[row.integration_id].append(
            FlowSchedule(
                id=str(row.id),
                name=row.name,
                disabled=row.disabled,
                schedule=row.schedule,
                last_executed_at=row.last_executed_at,
            )
        )

    # Topology rollup (step/router/lookup counts) -- same Lookup rule as
    # `flow_summaries`'s topo query (a processor whose adaptor is an
    # export), grouped by integration instead of by flow. Explicit tenant
    # scope on BOTH joined tables, same discipline as every other join here.
    topo_rows = (
        await db.execute(
            select(
                CeligoFlow.integration_id,
                func.count().label("steps"),
                # DISTINCT on the (flow, router) PAIR, not on router_id alone:
                # Celigo's router ids are unique within a flow, not within an
                # integration, so a cloned flow carries its original's router
                # ids verbatim. Counting router_id alone collapsed N cloned
                # flows' routers into one and under-reported the topology.
                # The FILTER is load-bearing: a row constructor whose members
                # are NULL is not itself NULL, so without it every router-less
                # step (most flow sources) would count `(flow_id, NULL)` as a
                # router of its own.
                func.count(distinct(tuple_(CeligoFlowStep.flow_id, CeligoFlowStep.router_id)))
                .filter(CeligoFlowStep.router_id.isnot(None))
                .label("routers"),
                func.count()
                .filter(and_(CeligoFlowStep.role == "processor", CeligoFlowStep.adaptor_type.ilike("%export")))
                .label("lookups"),
            )
            .select_from(CeligoFlowStep)
            .join(CeligoFlow, CeligoFlow.id == CeligoFlowStep.flow_id)
            .where(
                CeligoFlowStep.tenant_id == tenant_id,
                CeligoFlow.tenant_id == tenant_id,
                CeligoFlow.integration_id.in_(integration_ids),
            )
            .group_by(CeligoFlow.integration_id)
        )
    ).all()
    topo_by_integration = {row.integration_id: (row.steps, row.routers, row.lookups) for row in topo_rows}

    # Adaptor families -- DISTINCT adaptor_type per integration, mapped to a
    # coarse family in Python (`topology.adaptor_family`) and deduped there;
    # not a SQL-level aggregate because the family mapping is a Python rule,
    # not a column.
    adaptor_type_rows = (
        await db.execute(
            select(CeligoFlow.integration_id, CeligoFlowStep.adaptor_type)
            .distinct()
            .select_from(CeligoFlowStep)
            .join(CeligoFlow, CeligoFlow.id == CeligoFlowStep.flow_id)
            .where(
                CeligoFlowStep.tenant_id == tenant_id,
                CeligoFlow.tenant_id == tenant_id,
                CeligoFlow.integration_id.in_(integration_ids),
            )
        )
    ).all()
    families_by_integration: dict[uuid.UUID, set[str]] = defaultdict(set)
    for row in adaptor_type_rows:
        fam = adaptor_family(row.adaptor_type)
        if fam:
            families_by_integration[row.integration_id].add(fam)

    # Write mix -- same definition as `FlowSummary.writes`
    # (`record_type IS NOT NULL AND operation IS NOT NULL`), rolled up to the
    # integration instead of the flow.
    write_rows = (
        await db.execute(
            select(CeligoFlow.integration_id, CeligoFlowStep.record_type, func.count().label("write_count"))
            .select_from(CeligoFlowStep)
            .join(CeligoFlow, CeligoFlow.id == CeligoFlowStep.flow_id)
            .where(
                CeligoFlowStep.tenant_id == tenant_id,
                CeligoFlow.tenant_id == tenant_id,
                CeligoFlow.integration_id.in_(integration_ids),
                CeligoFlowStep.record_type.isnot(None),
                CeligoFlowStep.operation.isnot(None),
            )
            .group_by(CeligoFlow.integration_id, CeligoFlowStep.record_type)
            .order_by(func.count().desc(), CeligoFlowStep.record_type)
        )
    ).all()
    writes_by_integration: dict[uuid.UUID, list[RecordWrite]] = defaultdict(list)
    for row in write_rows:
        writes_by_integration[row.integration_id].append(
            RecordWrite(record_type=row.record_type, count=row.write_count)
        )

    # Scripts -- DISTINCT production script ids attached anywhere in the
    # integration (`celigo_script_is_production()`, tenant-scoped on every
    # joined table, same as `flow_summaries`'s equivalent query).
    scripts_rows = (
        await db.execute(
            select(CeligoFlow.integration_id, func.count(distinct(CeligoScriptAttachment.script_id)).label("scripts"))
            .select_from(CeligoScriptAttachment)
            .join(CeligoFlow, CeligoFlow.id == CeligoScriptAttachment.flow_id)
            .join(CeligoScript, CeligoScript.id == CeligoScriptAttachment.script_id)
            .where(
                CeligoScriptAttachment.tenant_id == tenant_id,
                CeligoFlow.tenant_id == tenant_id,
                CeligoScript.tenant_id == tenant_id,
                CeligoFlow.integration_id.in_(integration_ids),
                celigo_script_is_production(),
            )
            .group_by(CeligoFlow.integration_id)
        )
    ).all()
    scripts_by_integration = {row.integration_id: row.scripts for row in scripts_rows}

    # Open errors -- single-sourced via `celigo_error_is_open()`, same rule as
    # every other open-error count in this module.
    error_rows = (
        await db.execute(
            select(CeligoFlow.integration_id, func.count().label("errors"))
            .select_from(CeligoFlowError)
            .join(CeligoFlow, CeligoFlow.id == CeligoFlowError.flow_id)
            .where(
                CeligoFlowError.tenant_id == tenant_id,
                CeligoFlow.tenant_id == tenant_id,
                CeligoFlow.integration_id.in_(integration_ids),
                celigo_error_is_open(),
            )
            .group_by(CeligoFlow.integration_id)
        )
    ).all()
    errors_by_integration = {row.integration_id: row.errors for row in error_rows}

    # DISTINCT root causes across the whole integration -- same predicate as
    # `error_rows` above, grouped the same way, just counting distinct
    # `signature_id` instead of rows (mirrors the per-flow query at
    # `flow_detail`'s `flow_signature_count`). This is what lets the
    # tile's `ErrorPill` say "10 open · 1 root cause" instead of silently
    # defaulting to "10 open · 10 root causes" (Task 18).
    signature_rows = (
        await db.execute(
            select(
                CeligoFlow.integration_id,
                func.count(distinct(CeligoFlowError.signature_id)).label("signatures"),
            )
            .select_from(CeligoFlowError)
            .join(CeligoFlow, CeligoFlow.id == CeligoFlowError.flow_id)
            .where(
                CeligoFlowError.tenant_id == tenant_id,
                CeligoFlow.tenant_id == tenant_id,
                CeligoFlow.integration_id.in_(integration_ids),
                celigo_error_is_open(),
            )
            .group_by(CeligoFlow.integration_id)
        )
    ).all()
    signatures_by_integration = {row.integration_id: row.signatures for row in signature_rows}

    # Config changes in the last rolling 24h -- a coarse "something drifted
    # recently" signal, not a health verdict on its own.
    change_rows = (
        await db.execute(
            select(CeligoFlow.integration_id, func.count().label("changes"))
            .select_from(CeligoConfigChange)
            .join(CeligoFlow, CeligoFlow.id == CeligoConfigChange.flow_id)
            .where(
                CeligoConfigChange.tenant_id == tenant_id,
                CeligoFlow.tenant_id == tenant_id,
                CeligoFlow.integration_id.in_(integration_ids),
                CeligoConfigChange.created_at >= func.now() - text("interval '24 hours'"),
            )
            .group_by(CeligoFlow.integration_id)
        )
    ).all()
    changes_by_integration = {row.integration_id: row.changes for row in change_rows}

    # errors_checked_at rollup (migration 098): MIN across the integration's
    # flows, NULL if any of them is NULL -- `bool_or(... IS NULL)` decides
    # that in the same grouped query rather than a second Python pass, same
    # discipline as every other aggregate above.
    errors_checked_rows = (
        await db.execute(
            select(
                CeligoFlow.integration_id,
                func.min(CeligoFlow.errors_checked_at).label("min_checked_at"),
                func.bool_or(CeligoFlow.errors_checked_at.is_(None)).label("any_unchecked"),
            )
            .where(CeligoFlow.tenant_id == tenant_id, CeligoFlow.integration_id.in_(integration_ids))
            .group_by(CeligoFlow.integration_id)
        )
    ).all()
    errors_checked_by_integration: dict[uuid.UUID, datetime | None] = {
        row.integration_id: None if row.any_unchecked else row.min_checked_at for row in errors_checked_rows
    }

    out: list[IntegrationSummary] = []
    for i in integrations:
        bucket = buckets_by_integration.get(i.id)
        steps, routers, lookups = topo_by_integration.get(i.id, (0, 0, 0))
        out.append(
            IntegrationSummary(
                id=str(i.id),
                celigo_id=i.celigo_id,
                name=i.name,
                sandbox=i.sandbox,
                mode=i.mode,
                description=i.description,
                celigo_last_modified=i.celigo_last_modified,
                flow_count=bucket.flow_count if bucket else 0,
                scheduled_count=bucket.scheduled_count if bucket else 0,
                on_demand_count=bucket.on_demand_count if bucket else 0,
                paused_count=bucket.paused_count if bucket else 0,
                step_count=steps,
                router_count=routers,
                lookup_count=lookups,
                script_count=scripts_by_integration.get(i.id, 0),
                no_run_count=bucket.no_run_count if bucket else 0,
                error_count=errors_by_integration.get(i.id, 0),
                signature_count=signatures_by_integration.get(i.id, 0),
                changes_last_24h=changes_by_integration.get(i.id, 0),
                last_run_at=bucket.last_run_at if bucket else None,
                writes=writes_by_integration.get(i.id, []),
                adaptor_families=sorted(families_by_integration.get(i.id, set())),
                flow_schedules=schedules_by_integration.get(i.id, []),
                errors_checked_at=errors_checked_by_integration.get(i.id),
            )
        )
    return out


# ---------------------------------------------------------------------------
# sync_status -- moved from `get_sync_status`
# ---------------------------------------------------------------------------


async def sync_status(db: AsyncSession, *, tenant_id: uuid.UUID) -> SyncStatus:
    """The "Last synced" stats-strip value (mockup Screen 02). Reads
    `cursor_states` for the tenant's active Celigo connection; see
    `CeligoSyncStatusOut`'s docstring (celigo_flows.py) for the null
    semantics and why this is a separate endpoint rather than a field on
    `/integrations`.

    `cursor_states` has no `tenant_id` column of its own -- safe here only
    because `connection` is already tenant-verified by `_get_celigo_
    connection` before it's used to scope the cursor lookup."""
    connection = await _get_celigo_connection(db, tenant_id)
    if connection is None:
        return SyncStatus(last_synced_at=None)

    last_synced_at = (
        await db.execute(
            select(CursorState.last_synced_at).where(
                CursorState.connection_id == connection.id,
                CursorState.object_type == "celigo_flow_map",
            )
        )
    ).scalar_one_or_none()

    return SyncStatus(last_synced_at=last_synced_at)


# ---------------------------------------------------------------------------
# flow_summaries -- moved from `list_integration_flows`
# ---------------------------------------------------------------------------


async def flow_summaries(db: AsyncSession, *, tenant_id: uuid.UUID, integration_id: uuid.UUID) -> list[FlowSummary]:
    """Flows under one integration, each with its OPEN error/signature counts
    (one GROUP BY query for the whole list, not N+1 -- see `FlowSummary`'s
    sibling Out model, `CeligoFlowSummaryOut`, for the field-level rationale).
    `disabled` flows are never filtered out (mockup spec: paused flows stay
    visible, dimmed by the frontend on this flag).

    ASSUMES *integration_id* already resolved as a PRODUCTION, tenant-owned
    integration by the caller (the route 404s on that lookup itself, same as
    it always did) -- this function only needs the id to scope the flow
    query and, via the flows' own `celigo_connection_id`, the script
    divergence subquery below."""
    flows = (
        (
            await db.execute(
                select(CeligoFlow)
                .where(
                    CeligoFlow.tenant_id == tenant_id,
                    CeligoFlow.integration_id == integration_id,
                )
                .order_by(CeligoFlow.name)
            )
        )
        .scalars()
        .all()
    )
    if not flows:
        return []

    flow_ids = [f.id for f in flows]
    # Every flow under one integration is synced from that integration's own
    # connection -- so any one flow's `celigo_connection_id` is the value
    # `list_integration_flows` used to get off the resolved `integration` row.
    celigo_connection_id = flows[0].celigo_connection_id

    counts_result = await db.execute(
        select(
            CeligoFlowError.flow_id,
            func.count().label("error_count"),
            func.count(distinct(CeligoFlowError.signature_id)).label("signature_count"),
        )
        # "Open" is single-sourced in `celigo_error_is_open()` (whole-branch
        # review finding 5) so this query and errors.py's occurrence_count
        # recompute can never disagree on what it means again.
        .where(
            CeligoFlowError.tenant_id == tenant_id,
            CeligoFlowError.flow_id.in_(flow_ids),
            celigo_error_is_open(),
        )
        .group_by(CeligoFlowError.flow_id)
    )
    counts_by_flow: dict[uuid.UUID, tuple[int, int]] = {
        row.flow_id: (row.error_count, row.signature_count) for row in counts_result.all()
    }

    # Topology (step/router/branch/lookup counts), one GROUP BY query over
    # celigo_flow_steps for every flow in the integration at once. `lookups`
    # mirrors `topology.step_kind`'s Lookup rule (a processor whose adaptor
    # is an export) without importing that module's per-step projector --
    # this is a set-level count, not a per-step classification.
    topo_result = await db.execute(
        select(
            CeligoFlowStep.flow_id,
            func.count().label("steps"),
            func.count(distinct(CeligoFlowStep.router_id)).label("routers"),
            func.count(distinct(CeligoFlowStep.branch_id)).label("branches"),
            func.count()
            .filter(and_(CeligoFlowStep.role == "processor", CeligoFlowStep.adaptor_type.ilike("%export")))
            .label("lookups"),
        )
        .where(CeligoFlowStep.tenant_id == tenant_id, CeligoFlowStep.flow_id.in_(flow_ids))
        .group_by(CeligoFlowStep.flow_id)
    )
    topo_by_flow: dict[uuid.UUID, tuple[int, int, int, int]] = {
        row.flow_id: (row.steps, row.routers, row.branches, row.lookups) for row in topo_result.all()
    }

    # This flow's actual write mix: `record_type IS NOT NULL AND operation IS
    # NOT NULL` -- a lookup export step can carry `record_type` with no
    # `operation` (a read, not a write), which must not show up here. Ordered
    # by count desc then record_type in SQL so the frontend never has to sort.
    writes_result = await db.execute(
        select(CeligoFlowStep.flow_id, CeligoFlowStep.record_type, func.count().label("write_count"))
        .where(
            CeligoFlowStep.tenant_id == tenant_id,
            CeligoFlowStep.flow_id.in_(flow_ids),
            CeligoFlowStep.record_type.isnot(None),
            CeligoFlowStep.operation.isnot(None),
        )
        .group_by(CeligoFlowStep.flow_id, CeligoFlowStep.record_type)
        .order_by(func.count().desc(), CeligoFlowStep.record_type)
    )
    writes_by_flow: dict[uuid.UUID, list[RecordWrite]] = defaultdict(list)
    for row in writes_result.all():
        writes_by_flow[row.flow_id].append(RecordWrite(record_type=row.record_type, count=row.write_count))

    # Script family state: a family "diverged" when its PRODUCTION members'
    # distinct `content_hash` count is > 1 (a clone has drifted from its
    # original) -- `celigo_script_is_production()` on this subquery too
    # (gate finding, fix round 1): omitting it let a sandbox clone's hash
    # flag a family whose production copies actually agree, or vice versa,
    # exactly the "wrong by about half" failure mode that predicate's own
    # docstring warns about. Tenant- AND connection-scoped, so a
    # same-`dedup_key` coincidence under a different Celigo connection can
    # never leak in. This FILTERS on `content_hash` (never returns its
    # value) -- see this module's N2 docstring.
    diverged_keys = (
        select(CeligoScript.dedup_key)
        .where(
            CeligoScript.tenant_id == tenant_id,
            CeligoScript.celigo_connection_id == celigo_connection_id,
            CeligoScript.content_hash.isnot(None),
            celigo_script_is_production(),
        )
        .group_by(CeligoScript.dedup_key)
        .having(func.count(distinct(CeligoScript.content_hash)) > 1)
    )
    scripts_result = await db.execute(
        select(
            CeligoScriptAttachment.flow_id,
            func.count(distinct(CeligoScriptAttachment.script_id)).label("scripts"),
            func.count(distinct(CeligoScript.dedup_key))
            .filter(CeligoScript.dedup_key.in_(diverged_keys))
            .label("diverged"),
        )
        .join(CeligoScript, CeligoScript.id == CeligoScriptAttachment.script_id)
        .where(
            CeligoScriptAttachment.tenant_id == tenant_id,
            # Explicit tenant scope on BOTH joined tables (gate finding, fix
            # round 1) -- the `diverged_keys` subquery three lines above
            # already does this; RLS (FORCE) is the backstop, not the plan,
            # same discipline as every other join in this module.
            CeligoScript.tenant_id == tenant_id,
            CeligoScriptAttachment.flow_id.in_(flow_ids),
            celigo_script_is_production(),
        )
        .group_by(CeligoScriptAttachment.flow_id)
    )
    scripts_by_flow: dict[uuid.UUID, tuple[int, int]] = {
        row.flow_id: (row.scripts, row.diverged) for row in scripts_result.all()
    }

    return [
        FlowSummary(
            id=str(f.id),
            celigo_id=f.celigo_id,
            name=f.name,
            disabled=f.disabled,
            schedule=f.schedule,
            timezone=f.timezone,
            last_executed_at=f.last_executed_at,
            error_count=counts_by_flow.get(f.id, (0, 0))[0],
            signature_count=counts_by_flow.get(f.id, (0, 0))[1],
            step_count=topo_by_flow.get(f.id, (0, 0, 0, 0))[0],
            router_count=topo_by_flow.get(f.id, (0, 0, 0, 0))[1],
            branch_count=topo_by_flow.get(f.id, (0, 0, 0, 0))[2],
            lookup_count=topo_by_flow.get(f.id, (0, 0, 0, 0))[3],
            script_count=scripts_by_flow.get(f.id, (0, 0))[0],
            diverged_family_count=scripts_by_flow.get(f.id, (0, 0))[1],
            writes=writes_by_flow.get(f.id, []),
            celigo_last_modified=f.celigo_last_modified,
            errors_checked_at=f.errors_checked_at,
        )
        for f in flows
    ]


# ---------------------------------------------------------------------------
# flow_detail -- moved from `get_flow_detail`
# ---------------------------------------------------------------------------


async def flow_detail(db: AsyncSession, *, tenant_id: uuid.UUID, flow_id: uuid.UUID) -> FlowDetail | None:
    """One flow's steps (ordered, generators then top-level processors then
    router-branch processors -- `router_id`/`branch_id` break ties BETWEEN
    branches, `sequence` within each, and `celigo_id` last so the order is
    TOTAL even when Celigo omits a router id or a branch id -- see the query
    below's own comment; whole-branch review finding 7, re-review R2) plus
    every script attachment, nested onto the step it belongs to. Attachments
    with no owning step (a `routers[].script` ref -- belongs to the router,
    not a step) come back in `unassigned_attachments` instead of being
    dropped.

    Returns `None` for a flow that doesn't exist, belongs to another tenant,
    or lives under a sandbox integration (the production join below IS the
    filter) -- the caller (route) turns that into the 404."""
    # Production only, through the flow's integration (flows carry no flag
    # of their own) -- the join IS the filter.
    flow = (
        await db.execute(
            _join_production_integration(select(CeligoFlow), tenant_id).where(
                CeligoFlow.id == flow_id,
                CeligoFlow.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if flow is None:
        return None

    # WHOLE-BRANCH REVIEW FINDING 7: `ORDER BY sequence` alone does not
    # deliver the order this function's own docstring promises --
    # `extract_flow_steps` restarts `sequence` at 0 for EVERY branch (and for
    # the top-level arrays), so a generator and every router-branch processor
    # can share `sequence=0` with no tiebreaker, making render order
    # arbitrary and unstable across queries. `_step_order_priority` puts
    # generators first, top-level processors second, router-branch
    # processors last -- exactly the three groups the docstring names --
    # then `router_id`/`branch_id` break ties WITHIN the router-branch group
    # deterministically (two different branches both starting at sequence 0
    # would otherwise still tie), and `sequence` breaks ties within each
    # group/branch, as promised.
    #
    # SCOPED RE-REVIEW R2 (2026-08-27): those four keys are still not TOTAL,
    # proven on two tie shapes -- one router whose branches carry no
    # `branchId` (router_id equal, branch_id NULL on both), and two routers
    # carrying no `id` under one `branchId` (router_id NULL on both). Each
    # pair persists legitimately (`celigo_id` differs, so `branch_key`'s
    # unique constraint does not collapse them) and then ties on all four
    # keys, leaving render order arbitrary. `celigo_id` is appended as the
    # final key: NOT NULL, and unique within a flow's step set, so no two
    # rows can tie on the whole ordering any more.
    _step_order_priority = case(
        (CeligoFlowStep.role == "generator", 0),
        (and_(CeligoFlowStep.role == "processor", CeligoFlowStep.router_id.is_(None)), 1),
        else_=2,  # router-branch processor
    )
    steps = (
        (
            await db.execute(
                select(CeligoFlowStep)
                .where(
                    CeligoFlowStep.tenant_id == tenant_id,
                    CeligoFlowStep.flow_id == flow_id,
                )
                .order_by(
                    _step_order_priority,
                    CeligoFlowStep.router_id,
                    CeligoFlowStep.branch_id,
                    CeligoFlowStep.sequence,
                    CeligoFlowStep.celigo_id,
                )
            )
        )
        .scalars()
        .all()
    )
    attachments = (
        (
            await db.execute(
                select(CeligoScriptAttachment).where(
                    CeligoScriptAttachment.tenant_id == tenant_id,
                    CeligoScriptAttachment.flow_id == flow_id,
                )
            )
        )
        .scalars()
        .all()
    )

    # Script clone-family state (name/size/copies/versions/version-letter/
    # diverged), projected by `topology.script_family_facts` -- one query for
    # every attached script's whole family, never N+1. PRODUCTION ONLY: a
    # sandbox copy of a script must not be counted into a production
    # attachment's family state (see `celigo_script_is_production`'s
    # docstring for why this predicate belongs on every `celigo_scripts`
    # read, not just the by-id lookups). This selects whole `CeligoScript`
    # ORM rows (not an explicit `.content`/`.content_hash` projection) only
    # so `script_family_facts` can derive `size_chars`/`content_diverged`
    # from them -- see this module's N2 docstring.
    script_ids = {a.script_id for a in attachments if a.script_id is not None}
    facts: dict[uuid.UUID, ScriptFamilyFact] = {}
    if script_ids:
        dedup_keys = select(CeligoScript.dedup_key).where(
            CeligoScript.tenant_id == tenant_id,
            CeligoScript.id.in_(script_ids),
        )
        family_rows = (
            (
                await db.execute(
                    select(CeligoScript).where(
                        CeligoScript.tenant_id == tenant_id,
                        CeligoScript.celigo_connection_id == flow.celigo_connection_id,
                        CeligoScript.dedup_key.in_(dedup_keys),
                        celigo_script_is_production(),
                    )
                )
            )
            .scalars()
            .all()
        )
        facts = script_family_facts(family_rows)

    # Open (`celigo_error_is_open()`) error counts, per step and for the flow
    # as a whole. Per-step: ONE GROUP BY query for every step at once, never
    # N+1. `signature_count` is NOT `sum` of a per-step distinct count (that
    # would over-count a signature spanning multiple steps) -- it's DISTINCT
    # across the whole flow, hence the second, non-grouped query below with
    # the identical predicate.
    step_counts_result = await db.execute(
        select(
            CeligoFlowError.flow_step_id,
            func.count().label("error_count"),
        )
        .where(
            CeligoFlowError.tenant_id == tenant_id,
            CeligoFlowError.flow_id == flow.id,
            celigo_error_is_open(),
        )
        .group_by(CeligoFlowError.flow_step_id)
    )
    step_error_counts: dict[uuid.UUID | None, int] = {
        row.flow_step_id: row.error_count for row in step_counts_result.all()
    }
    flow_signature_count = (
        await db.execute(
            select(func.count(distinct(CeligoFlowError.signature_id))).where(
                CeligoFlowError.tenant_id == tenant_id,
                CeligoFlowError.flow_id == flow.id,
                celigo_error_is_open(),
            )
        )
    ).scalar_one()

    attachments_by_step: dict[uuid.UUID, list[Attachment]] = defaultdict(list)
    unassigned: list[Attachment] = []
    for a in attachments:
        fact = facts.get(a.script_id) if a.script_id is not None else None
        out = Attachment(
            id=str(a.id),
            flow_id=str(a.flow_id),
            flow_step_id=str(a.flow_step_id) if a.flow_step_id else None,
            script_id=str(a.script_id) if a.script_id else None,
            script_celigo_id=a.script_celigo_id,
            function_name=a.function_name,
            json_path=a.json_path,
            site_type=a.site_type,
            script_name=fact.name if fact else None,
            script_size_chars=fact.size_chars if fact else None,
            script_copies_count=fact.copies_count if fact else None,
            script_versions_count=fact.versions_count if fact else None,
            script_version_letter=fact.version_letter if fact else None,
            script_content_diverged=fact.content_diverged if fact else None,
        )
        if a.flow_step_id is not None:
            attachments_by_step[a.flow_step_id].append(out)
        else:
            unassigned.append(out)

    step_outs = [
        FlowStep(
            id=str(s.id),
            celigo_id=s.celigo_id,
            role=s.role,
            router_id=s.router_id,
            branch_id=s.branch_id,
            branch_key=s.branch_key,
            sequence=s.sequence,
            adaptor_type=s.adaptor_type,
            connection_celigo_id=s.connection_celigo_id,
            reference_name=s.reference_name,
            filter_json=s.filter_json,
            mapping_json=s.mapping_json,
            proceed_on_failure=s.proceed_on_failure,
            skip_retries=s.skip_retries,
            kind=step_kind(s.role, s.adaptor_type),
            record_type=s.record_type,
            operation=s.operation,
            search_id=s.search_id,
            attachments=attachments_by_step.get(s.id, []),
            error_count=step_error_counts.get(s.id, 0),
        )
        for s in steps
    ]

    raw_open_error_count = flow.raw_json.get("numOpenError") if isinstance(flow.raw_json, dict) else None
    # `not isinstance(..., bool)` is load-bearing: in Python `True` IS an int,
    # so a `numOpenError` of `true` would have been relayed as the count 1 --
    # an error total fabricated out of a field that said no such thing. This
    # is a shape nobody has seen, which is precisely why it must fail closed
    # to None rather than to a number a UI will then print as fact.
    celigo_open_error_count = (
        raw_open_error_count
        if isinstance(raw_open_error_count, int) and not isinstance(raw_open_error_count, bool)
        else None
    )
    # The SYNC's own parser, not a second one: `raw_json["lastErrorAt"]` is
    # the same Celigo wire value the repository already reads for
    # `lastModified`/`lastExecutedAt`, and it comes in both shapes that API
    # uses. This function used to reimplement only the string half, so an
    # epoch-ms value silently became NULL here while the sync's own columns
    # parsed it correctly.
    last_error_at = parse_celigo_timestamp(
        flow.raw_json.get("lastErrorAt") if isinstance(flow.raw_json, dict) else None
    )

    return FlowDetail(
        id=str(flow.id),
        integration_id=str(flow.integration_id),
        celigo_id=flow.celigo_id,
        name=flow.name,
        disabled=flow.disabled,
        schedule=flow.schedule,
        timezone=flow.timezone,
        last_executed_at=flow.last_executed_at,
        source_id=flow.source_id,
        ai_description_summary=flow.ai_description_summary,
        ai_description_detailed=flow.ai_description_detailed,
        celigo_last_modified=flow.celigo_last_modified,
        steps=step_outs,
        unassigned_attachments=unassigned,
        routers=project_routers(flow.raw_json),
        celigo_open_error_count=celigo_open_error_count,
        last_error_at=last_error_at,
        # Deliberately sums EVERY bucket, the `flow_step_id IS NULL` one
        # included -- see `CeligoFlowDetailOut.error_count`'s comment
        # (celigo_flows.py).
        error_count=sum(step_error_counts.values()),
        signature_count=flow_signature_count,
        errors_checked_at=flow.errors_checked_at,
    )


# ---------------------------------------------------------------------------
# flow_error_groups -- moved from `list_flow_errors`
# ---------------------------------------------------------------------------


async def flow_error_groups(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    flow_id: uuid.UUID,
    status: Literal["open", "resolved"],
) -> ErrorGroups:
    """A flow's errors, grouped by root-cause signature -- the "what's
    actually breaking" view a per-error list can't give.

    ASSUMES *flow_id* already resolved as a PRODUCTION, tenant-owned flow by
    the caller (the route 404s on that lookup itself, same production join
    `flow_detail` uses) -- the errors query below is independently
    tenant-scoped regardless, so this is a visibility/routing concern, not a
    security one.

    `status="open"` uses `celigo_error_is_open()` (single-sourced, same as
    every other open-count in this module); `status="resolved"` is
    `resolved_at IS NOT NULL` -- deliberately NOT "not open", since a row can
    be neither (purged but never resolved) and that state belongs to
    neither list, exactly per `celigo_error_is_open()`'s own docstring.

    Grouping happens in PYTHON, not SQL: each group needs both aggregates
    (first/last seen, retriable tri-state, distinct trace keys) AND a capped
    slice of the raw rows themselves, which a single GROUP BY can't produce
    together. Rows are capped at 2000 for grouping (a defensive ceiling, not
    a page size); the caller applies its own per-group `limit` when mapping
    to the response.

    That cap bounds `total` too: it counts the rows fetched, so a flow past
    2000 matching errors reports exactly 2000 and every group's `count` is
    likewise a count of fetched rows. This answers "what is breaking, and
    roughly how much of each", never "exactly how many errors does this flow
    have" -- `flow_detail`'s `error_count` is the uncapped aggregate for
    that."""
    open_predicate = celigo_error_is_open() if status == "open" else CeligoFlowError.resolved_at.isnot(None)

    errors = (
        (
            await db.execute(
                select(CeligoFlowError)
                .where(
                    CeligoFlowError.tenant_id == tenant_id,
                    CeligoFlowError.flow_id == flow_id,
                    open_predicate,
                )
                .order_by(CeligoFlowError.occurred_at.desc().nullslast(), CeligoFlowError.id.desc())
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )

    rows_by_signature: dict[uuid.UUID | None, list[CeligoFlowError]] = defaultdict(list)
    for e in errors:
        rows_by_signature[e.signature_id].append(e)

    signature_ids = [sid for sid in rows_by_signature if sid is not None]
    signatures_by_id: dict[uuid.UUID, CeligoErrorSignature] = {}
    if signature_ids:
        sig_rows = (
            (
                await db.execute(
                    select(CeligoErrorSignature).where(
                        CeligoErrorSignature.tenant_id == tenant_id,
                        CeligoErrorSignature.id.in_(signature_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        signatures_by_id = {s.id: s for s in sig_rows}

    groups: list[FlowErrorGroup] = []
    for sig_id, rows in rows_by_signature.items():
        occurred_ats = [r.occurred_at for r in rows if r.occurred_at is not None]
        purge_ats = [r.purge_at for r in rows if r.purge_at is not None]
        retriable_values = {r.retriable for r in rows if r.retriable is not None}
        # Tri-state: False if ANY row can't be retried (that's the operationally
        # relevant answer), True only if EVERY row can, None when nothing in the
        # group ever recorded a value either way.
        if False in retriable_values:
            group_retriable: bool | None = False
        elif True in retriable_values:
            group_retriable = True
        else:
            group_retriable = None

        trace_keys: list[str] = []
        seen_trace_keys: set[str] = set()
        step_ids: list[str | None] = []
        seen_step_ids: set[str | None] = set()
        for r in rows:
            if r.trace_key is not None and r.trace_key not in seen_trace_keys and len(trace_keys) < 25:
                seen_trace_keys.add(r.trace_key)
                trace_keys.append(r.trace_key)
            step_key = str(r.flow_step_id) if r.flow_step_id is not None else None
            if step_key not in seen_step_ids:
                seen_step_ids.add(step_key)
                step_ids.append(step_key)

        sig = signatures_by_id.get(sig_id) if sig_id is not None else None
        groups.append(
            FlowErrorGroup(
                signature=_signature_out(sig) if sig is not None else None,
                count=len(rows),
                step_ids=step_ids,
                first_seen_at=min(occurred_ats) if occurred_ats else None,
                last_seen_at=max(occurred_ats) if occurred_ats else None,
                retriable=group_retriable,
                purge_at=min(purge_ats) if purge_ats else None,
                trace_keys=trace_keys,
                # The caller's per-group display `limit` is applied by the
                # route mapping this onto the response, same split as the
                # 2000-row grouping cap above vs. the response-level `total`.
                errors=[_error_out(e) for e in rows],
            )
        )

    groups.sort(key=lambda g: g.count, reverse=True)

    return ErrorGroups(
        flow_id=str(flow_id),
        status=status,
        total=len(errors),
        groups=groups,
    )


# ---------------------------------------------------------------------------
# resolve_production_integration / resolve_production_flow -- task 3 (spec
# §3). The routes above never needed a fragment/ambiguity-aware lookup: every
# route takes a validated `uuid.UUID` path param and the ONE thing it can be
# wrong about is "not found" (404). A chat argument is free text, so these
# two additionally have to decide WHICH row(s) a name fragment or an exact
# name means before the caller can go looking for its data -- that "which
# row" decision belongs here, once, rather than duplicated in every one of
# the four tools that needs it (`mcp/tools/celigo_flow_map.py`).
# ---------------------------------------------------------------------------


def _try_parse_uuid(key: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(key)
    except (ValueError, AttributeError, TypeError):
        return None


async def resolve_production_integration(
    db: AsyncSession, *, tenant_id: uuid.UUID, key: str
) -> IntegrationSummaryRef | list[IntegrationSummaryRef] | None:
    """The one PRODUCTION integration *key* names, under the tenant's active
    Celigo connection -- *key* is either the integration's id or a
    case-insensitive fragment of its name (never an exact-match requirement;
    "ACME" must find "ACME ERP").

    Returns `None` for no match, a single `IntegrationSummaryRef` for exactly
    one, or a `list[IntegrationSummaryRef]` (every candidate, name order) when
    a fragment is ambiguous -- the caller (a tool's `execute`) turns that into
    an honest caveat naming the candidates rather than guessing one, per
    spec §3's `celigo.flows` argument. A `key` that parses as a UUID is
    always tried as an id lookup first (never as a name fragment, even if it
    also happens to look like one) -- ids are exact by construction, so
    there is nothing to disambiguate."""
    connection = await _get_celigo_connection(db, tenant_id)
    if connection is None:
        return None

    candidate_id = _try_parse_uuid(key)
    if candidate_id is not None:
        row = (
            await db.execute(
                select(CeligoIntegration.id, CeligoIntegration.name).where(
                    CeligoIntegration.id == candidate_id,
                    CeligoIntegration.tenant_id == tenant_id,
                    CeligoIntegration.celigo_connection_id == connection.id,
                    celigo_integration_is_production(),
                )
            )
        ).one_or_none()
        return IntegrationSummaryRef(id=str(row.id), name=row.name) if row else None

    rows = (
        await db.execute(
            select(CeligoIntegration.id, CeligoIntegration.name)
            .where(
                CeligoIntegration.tenant_id == tenant_id,
                CeligoIntegration.celigo_connection_id == connection.id,
                celigo_integration_is_production(),
                CeligoIntegration.name.ilike(f"%{key}%"),
            )
            .order_by(CeligoIntegration.name)
        )
    ).all()
    if not rows:
        return None
    if len(rows) == 1:
        return IntegrationSummaryRef(id=str(rows[0].id), name=rows[0].name)
    return [IntegrationSummaryRef(id=str(r.id), name=r.name) for r in rows]


async def resolve_production_flow(db: AsyncSession, *, tenant_id: uuid.UUID, key: str) -> list[FlowRef]:
    """Every PRODUCTION, tenant-owned flow *key* names -- *key* is either the
    flow's id or an EXACT case-insensitive match of its name (unlike the
    integration resolver above, never a fragment: a flow name collision is
    common enough across integrations, and within one, that a substring
    match would return far more candidates than a caller could usefully
    disambiguate). Empty when nothing matches; more than one element means
    the name is genuinely ambiguous (two flows -- typically in different
    integrations -- share it) and the caller must caveat rather than pick
    one. Same production join as `flow_detail`/`flow_error_groups`
    (`_join_production_integration`) -- a sandbox-integration flow is not a
    match here either, same as it is not found by id on those routes."""
    stmt = _join_production_integration(
        select(
            CeligoFlow.id,
            CeligoFlow.name,
            CeligoFlow.integration_id,
            CeligoIntegration.name.label("integration_name"),
            CeligoFlow.errors_checked_at,
        ),
        tenant_id,
    ).where(CeligoFlow.tenant_id == tenant_id)

    candidate_id = _try_parse_uuid(key)
    if candidate_id is not None:
        stmt = stmt.where(CeligoFlow.id == candidate_id)
    else:
        stmt = stmt.where(func.lower(CeligoFlow.name) == key.lower())

    rows = (await db.execute(stmt.order_by(CeligoFlow.name))).all()
    return [
        FlowRef(
            id=str(r.id),
            name=r.name,
            integration_id=str(r.integration_id),
            integration_name=r.integration_name,
            errors_checked_at=r.errors_checked_at,
        )
        for r in rows
    ]
