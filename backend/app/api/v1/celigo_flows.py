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
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, JsonValue
from sqlalchemy import and_, case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.connector_status import _get_celigo_connection
from app.core.database import get_db
from app.core.dependencies import require_feature, require_permission
from app.models.celigo import (
    CeligoErrorSignature,
    CeligoFlow,
    CeligoFlowError,
    CeligoFlowStep,
    CeligoIntegration,
    CeligoScript,
    CeligoScriptAttachment,
    celigo_error_is_open,
    celigo_integration_is_production,
    celigo_script_is_production,
)
from app.models.pipeline import CursorState
from app.models.user import User
from app.services.celigo.repository import list_logical_scripts
from app.services.celigo.topology import ScriptFamilyFact, project_routers, script_family_facts, step_kind

router = APIRouter(prefix="/celigo", tags=["celigo"])


def _join_production_integration(stmt, tenant_id):
    """INNER-join `CeligoIntegration` onto a statement whose FROM already
    carries `CeligoFlow`, with the tenant predicate AND
    `celigo_integration_is_production()` on the ON clause -- so the join is
    the production filter, not a WHERE someone has to remember. Used by the
    flow detail and the script sites query; one place to get it right."""
    return stmt.join(
        CeligoIntegration,
        and_(
            CeligoIntegration.id == CeligoFlow.integration_id,
            CeligoIntegration.tenant_id == tenant_id,
            celigo_integration_is_production(),
        ),
    )


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


def _parse_iso(value: object) -> datetime | None:
    """`raw_json["lastErrorAt"]` is Celigo's own ISO-8601 string (or absent) --
    never a `datetime` (raw_json is a JSONB blob). Fails closed to `None`
    rather than 500ing the flow on a shape nobody has seen yet."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class CeligoIntegrationOut(BaseModel):
    id: str
    celigo_id: str
    name: str
    sandbox: bool | None
    mode: str | None
    description: str | None
    celigo_last_modified: datetime | None


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
    (case-insensitive) -- Celigo's own "lookup" vocabulary (`topology.
    step_kind`), NOT the same predicate as `signature_count` above.
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
    # This app's OWN open counts (Task 4) -- `error_count` is the sum of every
    # step's `error_count` above (so it always agrees with what the steps show,
    # by construction, not by a second independent aggregate); `signature_count`
    # is DISTINCT root causes across the whole flow, which a per-step sum would
    # over-count when one signature spans multiple steps (see `get_flow_detail`'s
    # second, non-grouped query).
    error_count: int
    signature_count: int


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
    total: int
    groups: list[CeligoFlowErrorGroupOut]


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


@router.get("/integrations", response_model=list[CeligoIntegrationOut])
async def list_integrations(
    user: Annotated[User, Depends(require_permission("connections.view"))],
    _flag: Annotated[User, Depends(require_feature("celigo"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """PRODUCTION integrations synced under the tenant's currently active
    Celigo connection. Empty (never 404) when there is no active connection
    -- that is a legitimate "not connected yet" state, not an error.

    Sandbox integrations are excluded (operator directive 2026-09-01: "don't
    bring sandbox celigo, just production") through the ONE shared predicate,
    `celigo_integration_is_production()` -- see its docstring for the NULL
    rule and for why every other read in this module applies it too."""
    connection = await _get_celigo_connection(db, user.tenant_id)
    if connection is None:
        return []

    integrations = (
        (
            await db.execute(
                select(CeligoIntegration)
                .where(
                    CeligoIntegration.tenant_id == user.tenant_id,
                    CeligoIntegration.celigo_connection_id == connection.id,
                    celigo_integration_is_production(),
                )
                .order_by(CeligoIntegration.name)
            )
        )
        .scalars()
        .all()
    )
    return [
        CeligoIntegrationOut(
            id=str(i.id),
            celigo_id=i.celigo_id,
            name=i.name,
            sandbox=i.sandbox,
            mode=i.mode,
            description=i.description,
            celigo_last_modified=i.celigo_last_modified,
        )
        for i in integrations
    ]


# ---------------------------------------------------------------------------
# GET /celigo/sync-status
# ---------------------------------------------------------------------------


@router.get("/sync-status", response_model=CeligoSyncStatusOut)
async def get_sync_status(
    user: Annotated[User, Depends(require_permission("connections.view"))],
    _flag: Annotated[User, Depends(require_feature("celigo"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """The "Last synced" stats-strip value (mockup Screen 02) -- Task 9 had to
    drop it because nothing exposed it. Reads `cursor_states` for the
    tenant's active Celigo connection; see `CeligoSyncStatusOut`'s docstring
    for the null semantics and why this is a separate endpoint rather than a
    field on `/integrations`.

    `cursor_states` has no `tenant_id` column of its own -- safe here only
    because `connection` is already tenant-verified by `_get_celigo_
    connection` before it's used to scope the cursor lookup."""
    connection = await _get_celigo_connection(db, user.tenant_id)
    if connection is None:
        return CeligoSyncStatusOut(last_synced_at=None)

    last_synced_at = (
        await db.execute(
            select(CursorState.last_synced_at).where(
                CursorState.connection_id == connection.id,
                CursorState.object_type == "celigo_flow_map",
            )
        )
    ).scalar_one_or_none()

    return CeligoSyncStatusOut(last_synced_at=last_synced_at)


# ---------------------------------------------------------------------------
# GET /celigo/integrations/{id}/flows
# ---------------------------------------------------------------------------


@router.get("/integrations/{integration_id}/flows", response_model=list[CeligoFlowSummaryOut])
async def list_integration_flows(
    integration_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.view"))],
    _flag: Annotated[User, Depends(require_feature("celigo"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Flows under one integration, each with its OPEN error/signature counts
    (one GROUP BY query for the whole list, not N+1 -- see
    `CeligoFlowSummaryOut`'s docstring). The mockup leads with signature count
    ("3 root causes") and shows raw error count secondary; both are returned
    so the frontend never has to make a second call per flow to get either."""
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

    flows = (
        (
            await db.execute(
                select(CeligoFlow)
                .where(
                    CeligoFlow.tenant_id == user.tenant_id,
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
            CeligoFlowError.tenant_id == user.tenant_id,
            CeligoFlowError.flow_id.in_(flow_ids),
            celigo_error_is_open(),
        )
        .group_by(CeligoFlowError.flow_id)
    )
    counts_by_flow: dict[uuid.UUID, tuple[int, int]] = {
        row.flow_id: (row.error_count, row.signature_count) for row in counts_result.all()
    }

    # Task 5 -- topology (step/router/branch/lookup counts), one GROUP BY
    # query over celigo_flow_steps for every flow in the integration at once.
    # `lookups` mirrors `topology.step_kind`'s Lookup rule (a processor whose
    # adaptor is an export) without importing that module's per-step
    # projector -- this is a set-level count, not a per-step classification.
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
        .where(CeligoFlowStep.tenant_id == user.tenant_id, CeligoFlowStep.flow_id.in_(flow_ids))
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
            CeligoFlowStep.tenant_id == user.tenant_id,
            CeligoFlowStep.flow_id.in_(flow_ids),
            CeligoFlowStep.record_type.isnot(None),
            CeligoFlowStep.operation.isnot(None),
        )
        .group_by(CeligoFlowStep.flow_id, CeligoFlowStep.record_type)
        .order_by(func.count().desc(), CeligoFlowStep.record_type)
    )
    writes_by_flow: dict[uuid.UUID, list[CeligoRecordWriteOut]] = defaultdict(list)
    for row in writes_result.all():
        writes_by_flow[row.flow_id].append(CeligoRecordWriteOut(record_type=row.record_type, count=row.write_count))

    # Script family state: a family "diverged" when its members' distinct
    # `content_hash` count is > 1 (a clone has drifted from its original) --
    # tenant- AND connection-scoped, so a same-`dedup_key` coincidence under a
    # different Celigo connection can never leak in. `integration.
    # celigo_connection_id` is the tenant-verified connection this
    # integration (and therefore every flow under it) actually belongs to --
    # there is no separate `connection` row resolved in this endpoint.
    diverged_keys = (
        select(CeligoScript.dedup_key)
        .where(
            CeligoScript.tenant_id == user.tenant_id,
            CeligoScript.celigo_connection_id == integration.celigo_connection_id,
            CeligoScript.content_hash.isnot(None),
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
            CeligoScriptAttachment.tenant_id == user.tenant_id,
            CeligoScriptAttachment.flow_id.in_(flow_ids),
            celigo_script_is_production(),
        )
        .group_by(CeligoScriptAttachment.flow_id)
    )
    scripts_by_flow: dict[uuid.UUID, tuple[int, int]] = {
        row.flow_id: (row.scripts, row.diverged) for row in scripts_result.all()
    }

    return [
        CeligoFlowSummaryOut(
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
        )
        for f in flows
    ]


# ---------------------------------------------------------------------------
# GET /celigo/flows/{id}
# ---------------------------------------------------------------------------


@router.get("/flows/{flow_id}", response_model=CeligoFlowDetailOut)
async def get_flow_detail(
    flow_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.view"))],
    _flag: Annotated[User, Depends(require_feature("celigo"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """One flow's steps (ordered, generators then top-level processors then
    router-branch processors -- `router_id`/`branch_id` break ties BETWEEN
    branches, `sequence` within each, and `celigo_id` last so the order is
    TOTAL even when Celigo omits a router id or a branch id -- see the query
    below's own comment; whole-branch review finding 7, re-review R2) plus
    every script attachment, nested onto
    the step it belongs to. Attachments with no owning
    step (a `routers[].script` ref -- belongs to the router, not a step) come
    back in `unassigned_attachments` instead of being dropped."""
    # Production only, through the flow's integration (flows carry no flag
    # of their own) -- the join IS the filter.
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

    # WHOLE-BRANCH REVIEW FINDING 7: `ORDER BY sequence` alone does not
    # deliver the order this endpoint's own docstring promises --
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
                    CeligoFlowStep.tenant_id == user.tenant_id,
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
                    CeligoScriptAttachment.tenant_id == user.tenant_id,
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
    # read, not just the by-id lookups).
    script_ids = {a.script_id for a in attachments if a.script_id is not None}
    facts: dict[uuid.UUID, ScriptFamilyFact] = {}
    if script_ids:
        dedup_keys = select(CeligoScript.dedup_key).where(
            CeligoScript.tenant_id == user.tenant_id,
            CeligoScript.id.in_(script_ids),
        )
        family_rows = (
            (
                await db.execute(
                    select(CeligoScript).where(
                        CeligoScript.tenant_id == user.tenant_id,
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

    # Task 4 -- open (`celigo_error_is_open()`) error counts, per step and for
    # the flow as a whole. Per-step: ONE GROUP BY query for every step at
    # once, never N+1. `signature_count` is NOT `sum` of a per-step distinct
    # count (that would over-count a signature spanning multiple steps) --
    # it's DISTINCT across the whole flow, hence the second, non-grouped
    # query below with the identical predicate.
    step_counts_result = await db.execute(
        select(
            CeligoFlowError.flow_step_id,
            func.count().label("error_count"),
        )
        .where(
            CeligoFlowError.tenant_id == user.tenant_id,
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
                CeligoFlowError.tenant_id == user.tenant_id,
                CeligoFlowError.flow_id == flow.id,
                celigo_error_is_open(),
            )
        )
    ).scalar_one()

    attachments_by_step: dict[uuid.UUID, list[CeligoAttachmentOut]] = defaultdict(list)
    unassigned: list[CeligoAttachmentOut] = []
    for a in attachments:
        fact = facts.get(a.script_id) if a.script_id is not None else None
        out = CeligoAttachmentOut(
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
        CeligoFlowStepOut(
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
    celigo_open_error_count = raw_open_error_count if isinstance(raw_open_error_count, int) else None
    last_error_at = _parse_iso(flow.raw_json.get("lastErrorAt") if isinstance(flow.raw_json, dict) else None)

    return CeligoFlowDetailOut(
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
        routers=[CeligoRouterOut(**r) for r in project_routers(flow.raw_json)],
        celigo_open_error_count=celigo_open_error_count,
        last_error_at=last_error_at,
        error_count=sum(step_error_counts.values()),
        signature_count=flow_signature_count,
    )


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


@router.get("/flows/{flow_id}/errors", response_model=CeligoFlowErrorsOut)
async def list_flow_errors(
    flow_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.view"))],
    _flag: Annotated[User, Depends(require_feature("celigo"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: Literal["open", "resolved"] = Query("open", alias="status"),
    limit: int = Query(100, ge=1, le=500, description="Max errors returned per group, most recent first"),
):
    """A flow's errors (Task 4), grouped by root-cause signature -- the
    "what's actually breaking" view a per-error list can't give: `/celigo/errors`
    (above) already returns one signature's errors, but an operator opening a
    FLOW needs every signature attached to it at once, ranked by how many
    errors each is causing.

    Loads the flow through the exact same production join as `get_flow_detail`
    (404 for a flow that doesn't exist, belongs to another tenant, or lives
    under a sandbox integration) -- a flow a caller can't otherwise see must
    not leak its errors through this route either.

    `status=open` uses `celigo_error_is_open()` (single-sourced, same as every
    other open-count in this module); `status=resolved` is `resolved_at IS NOT
    NULL` -- deliberately NOT "not open", since a row can be neither (purged
    but never resolved) and that state belongs to neither list, exactly per
    `celigo_error_is_open()`'s own docstring.

    Grouping happens in PYTHON, not SQL: each group needs both aggregates
    (first/last seen, retriable tri-state, distinct trace keys) AND a capped
    slice of the raw rows themselves, which a single GROUP BY can't produce
    together. Rows are capped at 2000 for grouping (a defensive ceiling, not a
    page size -- a flow producing more open errors than that needs
    operational attention no client-side page could show usefully anyway);
    `limit` instead caps how many raw `errors` come back PER GROUP."""
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

    open_predicate = celigo_error_is_open() if status_filter == "open" else CeligoFlowError.resolved_at.isnot(None)

    errors = (
        (
            await db.execute(
                select(CeligoFlowError)
                .where(
                    CeligoFlowError.tenant_id == user.tenant_id,
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
                        CeligoErrorSignature.tenant_id == user.tenant_id,
                        CeligoErrorSignature.id.in_(signature_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        signatures_by_id = {s.id: s for s in sig_rows}

    groups: list[CeligoFlowErrorGroupOut] = []
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
            CeligoFlowErrorGroupOut(
                signature=CeligoErrorSignatureOut(
                    id=str(sig.id),
                    fingerprint=sig.fingerprint,
                    source=sig.source,
                    code=sig.code,
                    sample_message=sig.sample_message,
                    occurrence_count=sig.occurrence_count,
                    first_seen=sig.first_seen,
                    last_seen=sig.last_seen,
                )
                if sig is not None
                else None,
                count=len(rows),
                step_ids=step_ids,
                first_seen_at=min(occurred_ats) if occurred_ats else None,
                last_seen_at=max(occurred_ats) if occurred_ats else None,
                retriable=group_retriable,
                purge_at=min(purge_ats) if purge_ats else None,
                trace_keys=trace_keys,
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
                    for e in rows[:limit]
                ],
            )
        )

    groups.sort(key=lambda g: g.count, reverse=True)

    return CeligoFlowErrorsOut(
        flow_id=str(flow.id),
        status=status_filter,
        total=len(errors),
        groups=groups,
    )
