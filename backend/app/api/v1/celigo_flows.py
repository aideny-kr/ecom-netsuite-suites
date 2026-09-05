"""Task 8 — read APIs over the synced Celigo flow map (migration 094+095's
eight tables, Task 5's models + repository).

Every endpoint here is gated on `connections.view` AND `require_feature("celigo")`
-- mirrors the four `/connector-status/celigo*` endpoints in `connector_status.py`
exactly (Plan A established the flag must gate the API, not just the UI).

PII RULING (task-8 brief, not covered by the plan): `celigo_flow_errors.message`
and `celigo_error_signatures.sample_message` hold raw Celigo error text -- real
customer emails, order references. Returning them to an authenticated
SAME-TENANT admin is legitimate and necessary: an operator triaging a failed
flow needs to know which order broke, and stripping the message makes the
feature useless. `sanitizer.py`/`repository.py`'s "never log the message" rule
is about LOG LINES, a different threat (structured logs are searchable,
aggregated, and often shipped to a third party) -- it does not extend to an
authorized API response. This module still never logs either field itself, and
nothing here constructs an exception message or `logger.*(...)` call from
either column.

EXPLICIT RESPONSE MODELS, DELIBERATELY -- the single most important design
constraint in this file. Every response model below names every field by hand;
nothing uses `from_attributes=True`/`model_validate(orm_row)` against a mapped
class. An ORM auto-dump would make a column added to `app/models/celigo.py`
next month silently API-visible with no review of whether it's safe to expose
(`raw_json` on four of these tables is exactly the column that must never do
that). The explicit-field boundary here IS the review point.

EXPLICIT TENANT SCOPING ON EVERY QUERY AGAINST A tenant_id-BEARING TABLE --
RLS (FORCE, per `test_celigo_flow_map_rls.py`) is the backstop, not the
plan. Every SELECT below against one of the eight flow-map tables filters
on `tenant_id == user.tenant_id` in application code as well. ONE
exception, not an oversight (whole-branch review finding 11, 2026-08-27):
`get_sync_status`'s `CursorState` select has no tenant predicate at all,
because `cursor_states` has no `tenant_id` column to filter on -- see that
function's own docstring for why it is still safe (the `connection` row
scoping it is tenant-verified first, before it's ever used to build the
cursor lookup).

This module is READ-ONLY: no Celigo write verb, no mutation of any of the
eight tables. It does not call `app.services.celigo.repository`'s upsert/mark
functions -- only `list_logical_scripts` (a pure read helper) for script
clone-family collapsing, per that module's own docstring.

TASK 1 (extraction, 2026-09-04): `list_integrations`, `get_sync_status`,
`list_integration_flows`, `get_flow_detail` and `list_flow_errors` are now
thin adapters over `app.services.celigo.read_queries` -- that module owns
the query logic (moved verbatim, no behavior change) so the future chat
tools (`docs/superpowers/specs/2026-09-04-celigo-chat-access.md`) call the
SAME aggregation the pages do, not a second copy that can drift. Each
adapter's job is exactly: resolve the connection / 404, call the
`read_queries` function, map its dataclass onto this module's own Out model
(`dataclasses.asdict(dc)` unpacked into the Out model's constructor --
Pydantic validates/coerces nested dicts into the nested Out models itself,
so the mapping is mechanical, not hand-written per field). `_get_celigo_
connection` (originally `connector_status.py`) and `_join_production_
integration` moved into `read_queries.py` too; imported back here for the
routes (`get_script_detail`, `list_flow_changes`) that still need them
directly.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, JsonValue
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_feature, require_permission
from app.models.celigo import (
    CeligoConfigChange,
    CeligoErrorSignature,
    CeligoFlow,
    CeligoFlowError,
    CeligoFlowStep,
    CeligoIntegration,
    CeligoScript,
    CeligoScriptAttachment,
    celigo_integration_is_production,
    celigo_script_is_production,
)
from app.models.user import User
from app.services.celigo import read_queries
from app.services.celigo.read_queries import _join_production_integration
from app.services.celigo.repository import list_logical_scripts

router = APIRouter(prefix="/celigo", tags=["celigo"])


# ---------------------------------------------------------------------------
# Response schemas -- see module docstring: every field named by hand.
# ---------------------------------------------------------------------------

# A flow's `schedule`, relayed as the JSON Celigo sent. Typed as JSON rather
# than as a union of observed shapes, deliberately: the first cut declared
# `dict | None` off a fixture, and every integration with a scheduled flow
# 500d because the live value is a cron STRING ("? 0 */6 * * *" -- 96 of 239
# flows on the Framework account, 2026-09-01). Widening to `dict | str` would
# have repeated the same reasoning one member wider. This column mirrors
# whatever Celigo sends; the API's job is to relay it, not to vouch for its
# shape -- the frontend decides how to render what it gets.
CeligoSchedule = JsonValue


class CeligoFlowScheduleOut(BaseModel):
    """One row of `CeligoIntegrationOut.flow_schedules` -- the per-flow detail
    behind the integration card's aggregate schedule counts, so the frontend
    can list "which flows are on demand / paused / on what cron" without a
    second call per integration."""

    id: str
    name: str
    disabled: bool | None
    schedule: CeligoSchedule
    last_executed_at: datetime | None


class CeligoIntegrationOut(BaseModel):
    """Task 6 -- one request for the whole dashboard: every flow-schedule
    bucket, topology/script/write aggregate, and open-error/config-change
    count for this integration, each computed with one GROUP BY query across
    every integration at once (never N+1 per integration).

    `flow_count`/`paused_count`/`on_demand_count`/`scheduled_count` partition
    every flow under the integration into exactly one bucket: `disabled IS
    TRUE` is paused regardless of its schedule; among the rest, `celigo_flow_
    is_on_demand()` (models.py) decides on-demand vs scheduled -- so
    `scheduled_count + on_demand_count + paused_count == flow_count` always,
    by construction (each bucket's SQL filter is mutually exclusive with the
    other two, not derived by subtraction). `no_run_count` is flows whose
    `last_executed_at` is NULL; `last_run_at` is the MAX across all of them.
    `step_count`/`router_count`/`lookup_count` mirror `CeligoFlowSummaryOut`'s
    same-named fields, rolled up to the integration. `writes` and
    `adaptor_families` are set-level, not per-flow: `writes` sums a
    `(record_type, count)` pair across every flow in the integration (same
    write definition as `CeligoFlowSummaryOut.writes` --
    `record_type IS NOT NULL AND operation IS NOT NULL`); `adaptor_families`
    is the DISTINCT set of `topology.adaptor_family(adaptor_type)` results
    across every step in the integration, dropping `None`. `script_count` is
    DISTINCT production scripts attached anywhere in the integration.
    `error_count` is OPEN (`celigo_error_is_open()`); `signature_count` is the
    integration-wide twin of `CeligoFlowSummaryOut.signature_count` -- DISTINCT
    root causes across every flow in the integration (Task 18, cross-surface
    consistency: the tile's own `ErrorPill` used to default this to `error_count`
    itself when the field didn't exist, which read "10 open · 10 root causes"
    on this tile while the SAME 10 errors, one click away on the flows table or
    the flow page, correctly read "1 root cause" -- the same audit-trail rows,
    two different claims). `changes_last_24h` is
    `celigo_config_changes` rows in the last rolling 24h -- a coarse "has
    anything drifted recently" signal, not itself a health verdict.
    `flow_schedules` is a second, plain per-flow projection (no aggregation)
    for a drill-down list, not the buckets above."""

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
    writes: list[CeligoRecordWriteOut]
    adaptor_families: list[str]
    flow_schedules: list[CeligoFlowScheduleOut]
    errors_checked_at: datetime | None
    """The oldest check among this integration's flows (migration 098,
    `MIN(celigo_flows.errors_checked_at)`); NULL means at least one flow has
    never been checked (or the integration has no flows at all), so a zero
    open-error count anywhere in this response is not verified until this is
    set."""


class CeligoRecordWriteOut(BaseModel):
    """One `(record_type, count)` row of a flow's write mix -- see
    `CeligoFlowSummaryOut.writes`'s docstring."""

    record_type: str
    count: int


class CeligoFlowSummaryOut(BaseModel):
    """One row of `GET /celigo/integrations/{id}/flows`. `error_count`/
    `signature_count` are OPEN counts (`resolved_at IS NULL AND purged_at IS
    NULL`) computed with one GROUP BY query across every flow in the
    integration -- never N+1. `disabled` flows are never filtered out (mockup
    spec: paused flows stay visible, dimmed by the frontend on this flag).

    Task 5 -- topology/script/write aggregates for the flow-list table
    columns, each computed with one GROUP BY query across every flow in the
    integration, never N+1 (see `list_integration_flows`):
    `step_count`/`router_count`/`branch_count` come from one query over
    `celigo_flow_steps` (`COUNT(DISTINCT router_id)`/`COUNT(DISTINCT
    branch_id)` -- NULLs excluded by SQL's own `COUNT(DISTINCT ...)`
    semantics, which is exactly right: a step with no router/branch must not
    count as a router/branch of its own). `lookup_count` is steps whose role
    is `processor` and whose `adaptor_type` ends in "export"
    (case-insensitive) -- the same rule `topology.step_kind` uses to call a
    step a Lookup, restated here as a set-level GROUP BY count rather than
    imported per-step (this endpoint never classifies individual steps).
    `writes` is every `(record_type, count)` pair actually posted from this
    flow (`record_type IS NOT NULL AND operation IS NOT NULL` -- a step can
    carry a `record_type` from a lookup export with no `operation`, which is
    a read, not a write), ordered by count desc then record_type so the
    biggest write shows first. `script_count` is the DISTINCT script count
    attached to this flow (production scripts only, `celigo_script_is_
    production()`); `diverged_family_count` is how many of THIS flow's
    attached script families have more than one distinct `content_hash`
    across the family (a clone that has drifted from its original) -- see
    `list_integration_flows`'s `diverged_keys` subquery. Every new field
    defaults to 0 / `[]` for a flow with no steps/scripts, never omitted."""

    id: str
    celigo_id: str
    name: str
    disabled: bool | None
    schedule: CeligoSchedule
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
    writes: list[CeligoRecordWriteOut]
    celigo_last_modified: datetime | None
    errors_checked_at: datetime | None
    """When this flow's error summary was last consulted (migration 098);
    NULL = never checked with the correct endpoint, so a zero here is not a
    verified zero."""


class CeligoAttachmentOut(BaseModel):
    id: str
    flow_id: str
    flow_step_id: str | None
    script_id: str | None
    script_celigo_id: str
    function_name: str | None
    json_path: str
    site_type: str | None
    # Script clone-family state, projected by `topology.script_family_facts`
    # from the attachment's `script_id` -- `None` when the attachment's
    # script row isn't (yet) synced locally, or is a sandbox copy (see
    # `get_flow_detail`'s family query, which filters with
    # `celigo_script_is_production()`).
    script_name: str | None = None
    script_size_chars: int | None = None
    script_copies_count: int | None = None
    script_versions_count: int | None = None
    script_version_letter: str | None = None
    script_content_diverged: bool | None = None


class CeligoFlowStepOut(BaseModel):
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
    """Celigo's own export/import name; null until synced -- the UI must
    fall back, never invent."""
    # JsonValue, not `dict | None`: see CeligoSchedule's rationale above -- the
    # first cut declared these off a fixture too, and any shape other than a
    # plain object would 500 the whole flow the same way the schedule did.
    filter_json: JsonValue
    mapping_json: JsonValue
    proceed_on_failure: bool | None
    skip_retries: bool | None
    # Celigo's own vocabulary (`topology.step_kind`): Source (generator),
    # Lookup (a processor whose adaptor is an export), Destination (any
    # other processor).
    kind: str
    record_type: str | None
    operation: str | None
    search_id: str | None
    attachments: list[CeligoAttachmentOut]
    # Open (per `celigo_error_is_open()`) `celigo_flow_errors` count attributed
    # to THIS step -- Task 4. Computed by `get_flow_detail`'s per-step GROUP BY
    # query, never N+1; 0 when the step has no open error.
    error_count: int


class CeligoRouterBranchOut(BaseModel):
    id: str | None
    name: str | None
    rule_count: int
    next_router_id: str | None
    order: int
    declared_step_count: int


class CeligoRouterOut(BaseModel):
    """Projected from the synced flow object by `topology.project_routers` -- the
    declared side of branching (names, order, rules, chain, mode) that step rows
    cannot carry."""

    id: str | None
    name: str | None
    route_records_to: str | None
    route_records_using: str | None
    has_script_slot: bool
    branches: list[CeligoRouterBranchOut]


class CeligoFlowDetailOut(BaseModel):
    id: str
    integration_id: str
    celigo_id: str
    name: str
    disabled: bool | None
    schedule: CeligoSchedule
    timezone: str | None
    last_executed_at: datetime | None
    source_id: str | None
    ai_description_summary: str | None
    ai_description_detailed: str | None
    celigo_last_modified: datetime | None
    steps: list[CeligoFlowStepOut]
    # Attachments with no owning step -- e.g. a `routers[].script` ref, which
    # belongs to the router itself, not to any one page-generator/processor
    # step (see `app/models/celigo.py`'s `CeligoScriptAttachment` docstring).
    unassigned_attachments: list[CeligoAttachmentOut]
    routers: list[CeligoRouterOut]
    # Celigo's OWN open-error count/timestamp, echoed from `raw_json`
    # (`numOpenError`/`lastErrorAt`) -- distinct from this app's own
    # `celigo_flow_errors` count, which Task 4 adds. `None` when the flow's
    # raw object never carried the field (not yet observed, or omitted).
    celigo_open_error_count: int | None
    last_error_at: datetime | None
    # This app's OWN open counts (Task 4). `error_count` is EVERY open error on
    # the flow, including the ones no step owns: Celigo reports router-level and
    # pre-dispatch failures against the flow with a null `flow_step_id`, and the
    # flow total would understate reality if it dropped them. So the steps above
    # can sum to LESS than this number -- the difference is exactly the
    # unattributed bucket, and a UI that adds the bubbles up will not always
    # reach the header figure (pinned by
    # `TestFlowErrors::test_flow_error_count_includes_errors_no_step_owns`).
    # `signature_count` is DISTINCT root causes across the whole flow, which a
    # per-step sum would over-count when one signature spans multiple steps
    # (see `get_flow_detail`'s second, non-grouped query).
    error_count: int
    signature_count: int
    errors_checked_at: datetime | None
    """When this run last obtained Celigo's per-flow error summary for this
    flow (migration 098); NULL = never checked with the correct endpoint, so
    `error_count`/`celigo_open_error_count` being 0 is not a verified zero."""


class CeligoScriptAttachmentSiteOut(BaseModel):
    """One row of the script viewer's "Attached to / Where / Function" table
    (mockup spec, Screen 04). `script_celigo_id` names WHICH clone in the
    logical group was actually attached at this site -- clones can diverge, so
    this is not always the id the caller looked up. `integration_id` comes
    free off the same join that already gets `flow_name` (`CeligoFlow.
    integration_id` is a plain column, no extra query) -- it's what lets
    `CeligoScriptOut.integration_count` below answer the mockup's headline
    pill ("14 integrations") without a second round trip."""

    flow_id: str
    flow_name: str
    integration_id: str
    flow_step_id: str | None
    flow_step_role: str | None
    flow_step_adaptor_type: str | None
    script_celigo_id: str
    json_path: str
    function_name: str | None
    site_type: str | None


class CeligoScriptOut(BaseModel):
    """`content`/`content_hash`/`name` are THIS script row's own values -- the
    one the caller asked for by id, not necessarily the clone family's
    "representative" (`list_logical_scripts` picks a representative for
    display purposes in the LIST view; a clone's content can legitimately
    diverge from its original, per `content_diverged` below, so the detail
    view shows exactly the row the caller navigated to). `dedup_key`/
    `copies_count`/`attachment_count`/`content_diverged` describe the whole
    clone family this script belongs to (`app.services.celigo.repository.
    list_logical_scripts` -- reused verbatim, not reimplemented here).

    `integration_count` is the count of DISTINCT integrations across
    `used_by` -- computed in Python from the same rows `used_by` is already
    built from, not a second query. Deliberately NOT `integration_name` per
    site: the mockup's pill needs a count, not names, and `used_by` already
    carries `flow_name` for per-row context -- adding a name here would be an
    unused field, which is exactly what this file's explicit-response-model
    discipline exists to avoid."""

    id: str
    dedup_key: str
    name: str
    content: str | None
    content_hash: str | None
    copies_count: int
    attachment_count: int
    integration_count: int
    content_diverged: bool
    used_by: list[CeligoScriptAttachmentSiteOut]


class CeligoErrorOut(BaseModel):
    id: str
    celigo_id: str
    flow_id: str | None
    flow_step_id: str | None
    trace_key: str | None
    source: str | None
    code: str | None
    # PII -- see module docstring's PII ruling. Never logged.
    message: str | None
    occurred_at: datetime | None
    purge_at: datetime | None
    resolved_at: datetime | None
    purged_at: datetime | None
    retriable: bool | None


class CeligoErrorSignatureOut(BaseModel):
    id: str
    fingerprint: str
    source: str | None
    code: str | None
    # PII -- see module docstring's PII ruling. Never logged.
    sample_message: str | None
    occurrence_count: int
    first_seen: datetime | None
    last_seen: datetime | None


class CeligoErrorsResponse(BaseModel):
    signature: CeligoErrorSignatureOut
    errors: list[CeligoErrorOut]


class CeligoFlowErrorGroupOut(BaseModel):
    """One root cause's worth of a flow's errors (Task 4) -- grouped by
    `signature_id` in Python (never a SQL GROUP BY: the group also needs the
    raw rows themselves, capped at `limit`, which a GROUP BY can't return
    alongside its aggregates without a second query anyway). `signature` is
    `None` for the (rare, pre-classification) rows a signature was never
    assigned to -- still a real group, just an unclassified one."""

    signature: CeligoErrorSignatureOut | None
    count: int
    step_ids: list[str | None]
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    # None only when every row in the group has retriable=NULL; False if ANY
    # row is non-retriable (that is the operationally relevant answer -- "can
    # I safely retry this whole group" is only true when EVERY row is).
    retriable: bool | None
    purge_at: datetime | None
    # First 25 DISTINCT non-null trace keys, in first-seen order -- a cap, not
    # a promise every trace key in the group is listed (`count` is the true
    # total; this is enough to spot-check a handful of Celigo job runs).
    trace_keys: list[str]
    errors: list[CeligoErrorOut]


class CeligoFlowErrorsOut(BaseModel):
    flow_id: str
    status: Literal["open", "resolved"]
    # NOT the flow's whole-population count: it is the number of rows this
    # request actually grouped, and `list_flow_errors` caps that fetch at 2000.
    # A flow with more matching errors than the cap reports exactly 2000 here,
    # so a caller must not render this as "N errors on this flow" without
    # allowing for "at least". The flow's true open total is
    # `CeligoFlowDetailOut.error_count` (an uncapped aggregate).
    total: int
    groups: list[CeligoFlowErrorGroupOut]


class CeligoConfigChangeOut(BaseModel):
    """Task 7 -- one row of `celigo_config_changes` (see that model's own
    docstring for the polymorphic `object_kind`/`object_id`/`celigo_id` shape
    and why `old_value`/`new_value` are JSON rather than typed per field).
    `object_id` is stringified like every other id in this module; it carries
    no FK (the model has none either), so it is relayed as-is, never resolved
    or joined against here."""

    id: str
    object_kind: str
    object_id: str | None
    celigo_id: str
    field: str
    old_value: JsonValue
    new_value: JsonValue
    flow_id: str | None
    created_at: datetime


def _config_change_out(c: CeligoConfigChange) -> CeligoConfigChangeOut:
    return CeligoConfigChangeOut(
        id=str(c.id),
        object_kind=c.object_kind,
        object_id=str(c.object_id) if c.object_id is not None else None,
        celigo_id=c.celigo_id,
        field=c.field,
        old_value=c.old_value,
        new_value=c.new_value,
        flow_id=str(c.flow_id) if c.flow_id is not None else None,
        created_at=c.created_at,
    )


class CeligoSyncStatusOut(BaseModel):
    """`last_synced_at` is the freshness cursor Task 7's nightly sync worker
    writes to `cursor_states` (`object_type="celigo_flow_map"`) ONLY after a
    full sync completes without raising -- never a partial/failed run (see
    `app/workers/tasks/celigo_flow_map_sync.py`'s own docstring). Null covers
    BOTH "no active Celigo connection" and "connected, but no sync has ever
    completed" identically -- the stats strip has the same one thing to say
    either way: there is no successful sync to report a time for. A dedicated
    endpoint rather than a field folded onto `/integrations`: that endpoint
    returns a bare list (an existing frontend contract this fix must not
    break), and a bare JSON array has nowhere to carry a sibling field when
    the list itself is legitimately empty -- which "zero integrations synced
    yet" always is on a fresh connection, exactly when this timestamp matters
    most."""

    last_synced_at: datetime | None


# ---------------------------------------------------------------------------
# GET /celigo/integrations
# ---------------------------------------------------------------------------


def _integration_summary_out(i: read_queries.IntegrationSummary) -> CeligoIntegrationOut:
    return CeligoIntegrationOut(**dataclasses.asdict(i))


@router.get("/integrations", response_model=list[CeligoIntegrationOut])
async def list_integrations(
    user: Annotated[User, Depends(require_permission("connections.view"))],
    _flag: Annotated[User, Depends(require_feature("celigo"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """PRODUCTION integrations synced under the tenant's currently active
    Celigo connection -- see `read_queries.integration_summaries`'s own
    docstring for the query rationale (moved there verbatim, task 1); this
    adapter's only job is to map its dataclasses onto `CeligoIntegrationOut`."""
    summaries = await read_queries.integration_summaries(db, tenant_id=user.tenant_id)
    return [_integration_summary_out(s) for s in summaries]


# ---------------------------------------------------------------------------
# GET /celigo/sync-status
# ---------------------------------------------------------------------------


def _sync_status_out(s: read_queries.SyncStatus) -> CeligoSyncStatusOut:
    return CeligoSyncStatusOut(**dataclasses.asdict(s))


@router.get("/sync-status", response_model=CeligoSyncStatusOut)
async def get_sync_status(
    user: Annotated[User, Depends(require_permission("connections.view"))],
    _flag: Annotated[User, Depends(require_feature("celigo"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """The "Last synced" stats-strip value -- see
    `read_queries.sync_status`'s own docstring for the null semantics and
    the connection-scoping rationale (moved there verbatim, task 1)."""
    status = await read_queries.sync_status(db, tenant_id=user.tenant_id)
    return _sync_status_out(status)


# ---------------------------------------------------------------------------
# GET /celigo/integrations/{id}/flows
# ---------------------------------------------------------------------------


def _flow_summary_out(s: read_queries.FlowSummary) -> CeligoFlowSummaryOut:
    return CeligoFlowSummaryOut(**dataclasses.asdict(s))


@router.get("/integrations/{integration_id}/flows", response_model=list[CeligoFlowSummaryOut])
async def list_integration_flows(
    integration_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.view"))],
    _flag: Annotated[User, Depends(require_feature("celigo"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Flows under one integration -- see `read_queries.flow_summaries`'s own
    docstring for the aggregation rationale (moved there verbatim, task 1).
    This route keeps its own production-integration 404 lookup (hidden from
    the list must mean hidden by id too, same discipline as every other
    route in this module) and passes the validated `integration_id` through."""
    integration = (
        await db.execute(
            select(CeligoIntegration).where(
                CeligoIntegration.id == integration_id,
                CeligoIntegration.tenant_id == user.tenant_id,
                # A sandbox integration is not found by id either -- hidden
                # from the list must mean hidden, not merely unlisted.
                celigo_integration_is_production(),
            )
        )
    ).scalar_one_or_none()
    if integration is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found")

    summaries = await read_queries.flow_summaries(db, tenant_id=user.tenant_id, integration_id=integration_id)
    return [_flow_summary_out(s) for s in summaries]


# ---------------------------------------------------------------------------
# GET /celigo/flows/{id}
# ---------------------------------------------------------------------------


def _flow_detail_out(d: read_queries.FlowDetail) -> CeligoFlowDetailOut:
    return CeligoFlowDetailOut(**dataclasses.asdict(d))


@router.get("/flows/{flow_id}", response_model=CeligoFlowDetailOut)
async def get_flow_detail(
    flow_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.view"))],
    _flag: Annotated[User, Depends(require_feature("celigo"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """One flow's steps, script attachments and routers -- see
    `read_queries.flow_detail`'s own docstring for the ordering/attachment/
    error-count rationale (moved there verbatim, task 1). `None` there means
    not found, another tenant's row, or a sandbox integration; this route's
    only job is to turn that into the 404."""
    detail = await read_queries.flow_detail(db, tenant_id=user.tenant_id, flow_id=flow_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow not found")
    return _flow_detail_out(detail)


# ---------------------------------------------------------------------------
# GET /celigo/scripts/{id}
# ---------------------------------------------------------------------------


@router.get("/scripts/{script_id}", response_model=CeligoScriptOut)
async def get_script_detail(
    script_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.view"))],
    _flag: Annotated[User, Depends(require_feature("celigo"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """The script the caller asked for, plus its whole clone family's
    attachment sites collapsed into one `used_by` list -- reuses
    `list_logical_scripts` (Task 5) for the family grouping rather than
    reimplementing the clone-dedup rule (see `CeligoScriptOut`'s docstring for
    why `content` itself still comes from THIS row, not the family's
    "representative")."""
    script = (
        await db.execute(
            select(CeligoScript).where(
                CeligoScript.id == script_id,
                CeligoScript.tenant_id == user.tenant_id,
                celigo_script_is_production(),
            )
        )
    ).scalar_one_or_none()
    if script is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Script not found")

    logical_scripts = await list_logical_scripts(
        db, tenant_id=user.tenant_id, connection_id=script.celigo_connection_id
    )
    group = next((g for g in logical_scripts if g.dedup_key == script.dedup_key), None)
    # Defensive only -- `script` itself is a member of its own dedup group by
    # construction, so `group` is never actually None. Falls back to a
    # single-member view rather than a 500 if that invariant is ever violated.
    celigo_ids = group.celigo_ids if group is not None else (script.celigo_id,)
    content_diverged = group.content_diverged if group is not None else False
    # `attachment_count` is NOT taken from the group: it is `len(used_by)`
    # below, so the headline number and the list it summarises are one row
    # set by construction (gate round 3 found them computed by two queries
    # that agreed only when no sandbox site existed).

    # Tenant predicates on BOTH joined tables, not only on `CeligoScriptAttachment`
    # -- and on the JOIN's ON clause, not a trailing WHERE, which matters for
    # `CeligoFlowStep` specifically: it's an OUTER join (a router-level ref has
    # no owning step), so a WHERE-clause predicate would silently drop every
    # legitimate NULL-step row along with any cross-tenant one. RLS (FORCE) is
    # the backstop here, not the plan -- see module docstring -- and this
    # codebase's own test harness connects as a superuser, which bypasses RLS
    # unconditionally, so this explicit scoping is the only thing standing in
    # that context, not defence-in-depth.
    sites_stmt = select(
        CeligoScriptAttachment,
        CeligoFlow.name.label("flow_name"),
        CeligoFlow.integration_id.label("integration_id"),
        CeligoFlowStep.role.label("step_role"),
        CeligoFlowStep.adaptor_type.label("step_adaptor_type"),
    ).join(
        CeligoFlow,
        and_(
            CeligoFlow.id == CeligoScriptAttachment.flow_id,
            CeligoFlow.tenant_id == user.tenant_id,
        ),
    )
    # Production only: a site under a sandbox integration is not a site.
    sites_result = await db.execute(
        _join_production_integration(sites_stmt, user.tenant_id)
        .outerjoin(
            CeligoFlowStep,
            and_(
                CeligoFlowStep.id == CeligoScriptAttachment.flow_step_id,
                CeligoFlowStep.tenant_id == user.tenant_id,
            ),
        )
        .where(
            CeligoScriptAttachment.tenant_id == user.tenant_id,
            CeligoScriptAttachment.celigo_connection_id == script.celigo_connection_id,
            CeligoScriptAttachment.script_celigo_id.in_(celigo_ids),
        )
        .order_by(CeligoFlow.name, CeligoScriptAttachment.json_path)
    )

    used_by: list[CeligoScriptAttachmentSiteOut] = []
    integration_ids: set[uuid.UUID] = set()
    for attachment, flow_name, integration_id, step_role, step_adaptor_type in sites_result.all():
        integration_ids.add(integration_id)
        used_by.append(
            CeligoScriptAttachmentSiteOut(
                flow_id=str(attachment.flow_id),
                flow_name=flow_name,
                integration_id=str(integration_id),
                flow_step_id=str(attachment.flow_step_id) if attachment.flow_step_id else None,
                flow_step_role=step_role,
                flow_step_adaptor_type=step_adaptor_type,
                script_celigo_id=attachment.script_celigo_id,
                json_path=attachment.json_path,
                function_name=attachment.function_name,
                site_type=attachment.site_type,
            )
        )

    return CeligoScriptOut(
        id=str(script.id),
        dedup_key=script.dedup_key,
        name=script.name,
        content=script.content,
        content_hash=script.content_hash,
        copies_count=len(celigo_ids),
        attachment_count=len(used_by),
        integration_count=len(integration_ids),
        content_diverged=content_diverged,
        used_by=used_by,
    )


# ---------------------------------------------------------------------------
# GET /celigo/errors?signature=...
# ---------------------------------------------------------------------------


@router.get("/errors", response_model=CeligoErrorsResponse)
async def get_errors_for_signature(
    user: Annotated[User, Depends(require_permission("connections.view"))],
    _flag: Annotated[User, Depends(require_feature("celigo"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    signature: uuid.UUID = Query(..., description="celigo_error_signatures.id"),
    limit: int = Query(100, ge=1, le=500, description="Max errors returned, most recent first"),
):
    """One error signature (a normalized root cause) plus its raw occurrences.

    `signature` identifies the row by its OWN local id -- the same
    id-based-navigation shape as `/celigo/flows/{id}` and `/celigo/scripts/{id}`
    (a value the caller only ever has because a prior response handed it back),
    not the `fingerprint` hash, which is an internal computed value never
    meant as an external lookup key."""
    sig = (
        await db.execute(
            select(CeligoErrorSignature).where(
                CeligoErrorSignature.id == signature,
                CeligoErrorSignature.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if sig is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Error signature not found")

    errors = (
        (
            await db.execute(
                select(CeligoFlowError)
                .where(
                    CeligoFlowError.tenant_id == user.tenant_id,
                    CeligoFlowError.signature_id == signature,
                )
                .order_by(CeligoFlowError.occurred_at.desc().nullslast(), CeligoFlowError.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return CeligoErrorsResponse(
        signature=CeligoErrorSignatureOut(
            id=str(sig.id),
            fingerprint=sig.fingerprint,
            source=sig.source,
            code=sig.code,
            sample_message=sig.sample_message,
            occurrence_count=sig.occurrence_count,
            first_seen=sig.first_seen,
            last_seen=sig.last_seen,
        ),
        errors=[
            CeligoErrorOut(
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
            for e in errors
        ],
    )


# ---------------------------------------------------------------------------
# GET /celigo/flows/{id}/errors
# ---------------------------------------------------------------------------


def _flow_error_group_out(g: read_queries.FlowErrorGroup, *, limit: int) -> CeligoFlowErrorGroupOut:
    data = dataclasses.asdict(g)
    # Per-group display cap -- the raw `errors` list, not `count` (the true
    # total): see `read_queries.flow_error_groups`'s own docstring for why
    # this trim lives in the adapter, not the query function.
    data["errors"] = data["errors"][:limit]
    return CeligoFlowErrorGroupOut(**data)


def _flow_errors_out(groups: read_queries.ErrorGroups, *, limit: int) -> CeligoFlowErrorsOut:
    return CeligoFlowErrorsOut(
        flow_id=groups.flow_id,
        status=groups.status,
        total=groups.total,
        groups=[_flow_error_group_out(g, limit=limit) for g in groups.groups],
    )


@router.get("/flows/{flow_id}/errors", response_model=CeligoFlowErrorsOut)
async def list_flow_errors(
    flow_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.view"))],
    _flag: Annotated[User, Depends(require_feature("celigo"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: Literal["open", "resolved"] = Query("open", alias="status"),
    limit: int = Query(100, ge=1, le=500, description="Max errors returned per group, most recent first"),
):
    """A flow's errors, grouped by root-cause signature -- see
    `read_queries.flow_error_groups`'s own docstring for the grouping/cap
    rationale (moved there verbatim, task 1). Loads the flow through the
    exact same production join `get_flow_detail` uses (404 for a flow that
    doesn't exist, belongs to another tenant, or lives under a sandbox
    integration) -- a flow a caller can't otherwise see must not leak its
    errors through this route either."""
    flow = (
        await db.execute(
            _join_production_integration(select(CeligoFlow), user.tenant_id).where(
                CeligoFlow.id == flow_id,
                CeligoFlow.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if flow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow not found")

    groups = await read_queries.flow_error_groups(
        db, tenant_id=user.tenant_id, flow_id=flow_id, status=status_filter, errors_limit=limit
    )
    return _flow_errors_out(groups, limit=limit)


# ---------------------------------------------------------------------------
# GET /celigo/integrations/{id}/changes
# ---------------------------------------------------------------------------


@router.get("/integrations/{integration_id}/changes", response_model=list[CeligoConfigChangeOut])
async def list_integration_changes(
    integration_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.view"))],
    _flag: Annotated[User, Depends(require_feature("celigo"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(200, ge=1, le=500, description="Max changes returned, most recent first"),
):
    """Every drift event (Task 7) attributed to a flow under this integration,
    newest first. Resolves the integration through the same production
    lookup `list_integration_flows` uses (404 for another tenant's row or a
    sandbox integration -- hidden means hidden by id too, same discipline as
    every other route in this module), then scopes `celigo_config_changes` to
    flows under it via `flow_id IN (...)`. A 'script'-kind change (`flow_id`
    IS NULL -- a script can be attached from many flows or none, see the
    model's own docstring) is therefore never returned by THIS route; it has
    no single owning integration to attribute it to."""
    integration = (
        await db.execute(
            select(CeligoIntegration).where(
                CeligoIntegration.id == integration_id,
                CeligoIntegration.tenant_id == user.tenant_id,
                celigo_integration_is_production(),
            )
        )
    ).scalar_one_or_none()
    if integration is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found")

    flow_ids = select(CeligoFlow.id).where(
        CeligoFlow.tenant_id == user.tenant_id,
        CeligoFlow.integration_id == integration_id,
    )
    changes = (
        (
            await db.execute(
                select(CeligoConfigChange)
                .where(
                    CeligoConfigChange.tenant_id == user.tenant_id,
                    CeligoConfigChange.flow_id.in_(flow_ids),
                )
                .order_by(CeligoConfigChange.created_at.desc(), CeligoConfigChange.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [_config_change_out(c) for c in changes]


# ---------------------------------------------------------------------------
# GET /celigo/flows/{id}/changes
# ---------------------------------------------------------------------------


@router.get("/flows/{flow_id}/changes", response_model=list[CeligoConfigChangeOut])
async def list_flow_changes(
    flow_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.view"))],
    _flag: Annotated[User, Depends(require_feature("celigo"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(200, ge=1, le=500, description="Max changes returned, most recent first"),
):
    """This flow's own drift events (Task 7) plus its steps' ('flow' and
    'flow_step' kinds both carry THIS flow's id -- see the model's own
    docstring), newest first. Resolves the flow through the same production
    join `get_flow_detail`/`list_flow_errors` use (404 for another tenant's
    row or a flow under a sandbox integration)."""
    flow = (
        await db.execute(
            _join_production_integration(select(CeligoFlow), user.tenant_id).where(
                CeligoFlow.id == flow_id,
                CeligoFlow.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if flow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow not found")

    changes = (
        (
            await db.execute(
                select(CeligoConfigChange)
                .where(
                    CeligoConfigChange.tenant_id == user.tenant_id,
                    CeligoConfigChange.flow_id == flow_id,
                )
                .order_by(CeligoConfigChange.created_at.desc(), CeligoConfigChange.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [_config_change_out(c) for c in changes]
