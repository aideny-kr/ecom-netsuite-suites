"""Task 7: nightly Celigo flow-map sync + drift detection.

Modeled on `app/services/suitescript_sync_service.py`'s freshness discipline
(a `last_sync_at`-style cursor only advances on a fully successful run --
never on partial failure) crossed with `app/workers/tasks/
netsuite_deposit_sync_all.py` / `stripe_sync_all.py`'s `DISPATCHABLE_
CONNECTION_STATUSES` fan-out (dispatch even a `status='error'` connection so
its own failure is loud, per the 2026-07-29 incident). This module is the
pure orchestration half; `app/workers/tasks/celigo_flow_map_sync.py` is the
Celery-task half that owns the freshness cursor itself (see that module's
docstring for why -- `celigo_write_guard.py` refuses writing the cursor onto
the `connections` row, so it lives on `cursor_states` instead).

SEQUENCING (brief: "integrations -> flows -> steps -> scripts -> errors per
step", extended by fix round 1 -- see below):
  Phase A: every integration (`client.list_resource("integration")`).
  Phase B: every flow (`client.list_resource("flow")`); for EACH flow, its
    steps are extracted (`repository.extract_flow_steps`) and upserted
    immediately after the flow itself. Flows and their own steps cannot be
    two fully separate phases -- Celigo has no "list steps" endpoint; a step
    only exists inside its flow's own payload -- so read this as one combined
    "flows -> steps" phase, per-flow. The flow object ITSELF is also walked
    for script refs here (`graph.walk_script_refs`) -- routers can carry
    `script` (see Phase D's docstring note for why this is separate from the
    export/import walk below).
  Phase C: every script (`client.list_resource("script")`), independent of
    flow order.
  Phase D (FIX ROUND 1, added after the first cut of this module shipped):
    every export AND import (`client.list_resource("export"/"import")`).
    THE REASON THIS EXISTS: the plan's own live-probed Verified Facts say
    `transform.script` is the MOST-USED script attachment site in the real
    account, and `transform`/`filter` live on the EXPORT/IMPORT object, NOT
    on the flow -- a flow only references an export/import BY ID inside
    `pageGenerators`/`pageProcessors`. Walking only flow objects (as the
    first cut of this module did) finds approximately nothing; the refs are
    on objects that were never fetched. One missing stage explained three
    separately-flagged gaps: `celigo_script_attachments` never populated,
    and `celigo_flow_steps.adaptor_type`/`connection_celigo_id` unfillable
    (both flagged by Task 4 AND Task 5, since those two columns live on the
    REFERENCED export/import object per `upsert_flow_step`'s own docstring).
    For each fetched export/import: (a) EVERY matching `celigo_flow_steps`
    row (the same export id can be referenced by more than one flow, or more
    than one branch within one flow) is backfilled with `adaptor_type`/
    `connection_celigo_id` (`repository.backfill_flow_step_reference_info`);
    (b) it is walked for script refs and each one is recorded as a
    `celigo_script_attachments` row PER FLOW that references it (an export
    used by 3 flows produces 3 rows -- see `_process_reference_object`'s
    docstring for exactly how the one-export-many-flows case is modelled).
    Uses `list_resource`'s collection-listing pattern (pagination + sanitize
    for free), NOT per-id fetches -- same reasoning as Phase C's scripts.
    FIX ROUND 2: the SAME backfill call also carries Task 11's provenance --
    `record_type`/`operation`/`search_id` (`_extract_provenance`, migration
    096) -- for the identical reason: Task 11 derives "which flows write
    which NetSuite record types" from `netsuite_da.recordType`/`operation`
    (imports) and `netsuite.restlet.recordType`/`searchId` (exports), fields
    this phase already fetches but, before this round, threw away.
  Phase E: for every step collected during Phase B, in that same order, its
    open errors (`client.list_flow_errors_for_step`) are fetched and
    snapshotted (`errors.upsert_errors`). This is deliberately its OWN pass,
    after every step already exists as a real `celigo_flow_steps` row --
    `upsert_errors` needs a real `step.id`/`step.flow_id` (errors.py's own
    docstring; Task 6's report flagged this as never exercised end-to-end
    with a real orchestrator -- this module is the first real consumer, see
    `_StepRef` below for how).
  Purge marking runs last, once per connection, independent of any single
  step's sync (`repository.mark_flow_errors_purged`'s own docstring).

ANY exception anywhere in this walk propagates uncaught -- no broad
try/except swallows it. That is what makes "the freshness cursor advances
only on success" true from the caller's side: `celigo_flow_map_sync.py` only
persists the cursor AFTER `sync_flow_map_for_connection` returns without
raising, so a failure anywhere in the multi-stage walk (auth rejected, a
malformed response, a role-collision, a DB constraint violation) leaves the
cursor exactly where the LAST fully successful run left it. This is stricter
than Stripe's/NetSuite's "partial success still completes" posture --
deliberately: this task's own brief states the rule unqualified ("a partial
failure must not advance it"), unlike those two services' documented
graceful-degradation contracts.

WHAT THIS DELIBERATELY STILL DOES NOT DO:
  * `_stepId` (the query param `client.list_flow_errors_for_step` sends) is
    populated with a step's OWN `celigo_id` -- the referenced export/import
    id, the only Celigo-native id a step has anywhere in this schema. Task
    3's own report flags `_stepId` as "inferred from the MCP tool's schema,
    never confirmed against a raw REST call" -- this module inherits that
    same unverified assumption, not a new one; see its report for the
    reasoning that produced it.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.celigo import CeligoFlow, CeligoFlowError, CeligoFlowStep, CeligoScript
from app.services.celigo.client import get_resource, list_flow_errors_for_step, list_resource
from app.services.celigo.errors import upsert_errors
from app.services.celigo.graph import ScriptRef, walk_script_refs
from app.services.celigo.repository import (
    backfill_flow_step_reference_info,
    extract_flow_steps,
    insert_config_change,
    mark_flow_errors_purged,
    upsert_flow,
    upsert_flow_step,
    upsert_integration,
    upsert_script,
    upsert_script_attachment_from_ref,
)

_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=30.0)

# Kinds fetched by the export/import phase (Phase D) -- both carry
# transform/filter, the two confirmed script-attachment sites that live off
# the flow object. See module docstring's Phase D entry.
_REFERENCE_OBJECT_KINDS = ("export", "import")


@dataclass
class SyncSummary:
    """Return value of `sync_flow_map_for_connection`. Every count is what
    was WRITTEN this run, not a Celigo-reported total (matches this
    codebase's other sync summary dataclasses, e.g. `DepositSyncResult`)."""

    integrations_synced: int = 0
    flows_synced: int = 0
    flows_skipped_no_integration: int = 0
    steps_synced: int = 0
    scripts_synced: int = 0
    exports_imports_synced: int = 0
    exports_imports_skipped_no_flow: int = 0
    flow_steps_backfilled: int = 0
    attachments_synced: int = 0
    steps_with_errors_checked: int = 0
    errors_snapshotted: int = 0
    config_changes_recorded: int = 0
    errors_purged: int = 0


@dataclass(frozen=True)
class _StepRef:
    """Lightweight duck-typed stand-in for `errors.upsert_errors`'s `step`
    param (that function needs only `.id`/`.flow_id` -- see its own
    docstring). Built from values already in hand right after
    `repository.upsert_flow_step` returns a bare id (Task 6's report flagged
    that repository.py's upserts return ids, not rows, so a caller needs
    "either an extra SELECT or the ORM object"). This is a third option
    neither anticipated: hold just the two id values `upsert_errors` actually
    reads, no round-trip SELECT and no full ORM row required."""

    id: uuid.UUID
    flow_id: uuid.UUID


def _hash_content(content: str | None) -> str | None:
    """Independent copy of `repository._content_hash`'s algorithm (sha256 hex
    digest of the UTF-8 content) -- duplicated, not imported, matching this
    branch's established precedent (`errors.py`'s `_parse_timestamp`
    docstring: "this module owns its own parsing... the same way repository
    .py owns parsing of flow/script fields"). Used to compare an incoming
    script's would-be hash against the STORED one before `upsert_script`
    overwrites it -- the two algorithms must produce identical output for the
    comparison to mean anything, which is why this is a byte-for-byte copy,
    not a reinterpretation."""
    if content is None:
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _flow_drift(existing: CeligoFlow | None, sanitized_flow: dict) -> list[tuple[str, object, object]]:
    """Diff `disabled`/`schedule` against the STORED row. Called with the row
    fetched BEFORE `upsert_flow` overwrites it (see
    `sync_flow_map_for_connection`). Returns `[]` -- not an error -- when
    there is no existing row: a flow's first-ever sync has nothing to diff
    against, and "first observed" is not drift."""
    if existing is None:
        return []
    changes: list[tuple[str, object, object]] = []
    new_disabled = sanitized_flow.get("disabled")
    if existing.disabled != new_disabled:
        changes.append(("disabled", existing.disabled, new_disabled))
    new_schedule = sanitized_flow.get("schedule")
    if existing.schedule != new_schedule:
        changes.append(("schedule", existing.schedule, new_schedule))
    return changes


def _step_drift(existing: CeligoFlowStep | None, step_input) -> list[tuple[str, object, object]]:
    """Diff `mapping_json`/`filter_json` against the STORED row. Same
    first-sync-is-not-drift posture as `_flow_drift`."""
    if existing is None:
        return []
    changes: list[tuple[str, object, object]] = []
    if existing.mapping_json != step_input.mapping_json:
        changes.append(("mapping_json", existing.mapping_json, step_input.mapping_json))
    if existing.filter_json != step_input.filter_json:
        changes.append(("filter_json", existing.filter_json, step_input.filter_json))
    return changes


def _script_drift(existing: CeligoScript | None, new_content_hash: str | None) -> list[tuple[str, object, object]]:
    """`content_hash` drift is recorded ONLY when both the stored and the new
    hash are real values. A `None` on either side means "we don't know", not
    "it changed to nothing" -- a list-mode script fetch can omit `content`
    entirely (observed-shapes.md), and a fetch-mode difference must never be
    reported as a content edit."""
    if existing is None or existing.content_hash is None or new_content_hash is None:
        return []
    if existing.content_hash == new_content_hash:
        return []
    return [("content_hash", existing.content_hash, new_content_hash)]


async def _record_drift(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    object_kind: str,
    object_id: uuid.UUID,
    celigo_id: str,
    flow_id: uuid.UUID | None,
    changes: list[tuple[str, object, object]],
) -> None:
    for field_name, old_value, new_value in changes:
        await insert_config_change(
            db,
            tenant_id=tenant_id,
            connection_id=connection_id,
            object_kind=object_kind,
            object_id=object_id,
            celigo_id=celigo_id,
            flow_id=flow_id,
            field=field_name,
            old_value=old_value,
            new_value=new_value,
        )


async def _get_existing_flow(db: AsyncSession, *, tenant_id, connection_id, celigo_id: str) -> CeligoFlow | None:
    return (
        await db.execute(
            select(CeligoFlow).where(
                CeligoFlow.tenant_id == tenant_id,
                CeligoFlow.celigo_connection_id == connection_id,
                CeligoFlow.celigo_id == celigo_id,
            )
        )
    ).scalar_one_or_none()


async def _get_existing_step(
    db: AsyncSession, *, tenant_id, connection_id, flow_id, celigo_id: str, branch_key: str
) -> CeligoFlowStep | None:
    return (
        await db.execute(
            select(CeligoFlowStep).where(
                CeligoFlowStep.tenant_id == tenant_id,
                CeligoFlowStep.celigo_connection_id == connection_id,
                CeligoFlowStep.flow_id == flow_id,
                CeligoFlowStep.celigo_id == celigo_id,
                CeligoFlowStep.branch_key == branch_key,
            )
        )
    ).scalar_one_or_none()


async def _get_existing_script(db: AsyncSession, *, tenant_id, connection_id, celigo_id: str) -> CeligoScript | None:
    return (
        await db.execute(
            select(CeligoScript).where(
                CeligoScript.tenant_id == tenant_id,
                CeligoScript.celigo_connection_id == connection_id,
                CeligoScript.celigo_id == celigo_id,
            )
        )
    ).scalar_one_or_none()


async def _resolve_integration_id(
    db: AsyncSession,
    *,
    tenant_id,
    connection_id,
    integration_ids: dict[str, uuid.UUID],
    celigo_integration_id: str | None,
    token: str,
    region: str,
    http: httpx.AsyncClient,
) -> uuid.UUID | None:
    """Local id for `celigo_integration_id`, from this run's own Phase A map
    when present. Falls back to an on-demand `get_resource` + upsert for the
    rare case a flow references an integration Phase A's listing didn't
    return -- never silently drop the flow over a listing gap. Returns `None`
    ONLY when `celigo_integration_id` itself is falsy (a malformed flow, no
    id to even try) -- a genuine fetch failure for a REAL id propagates
    uncaught, same as everything else in this module (no swallowed
    exceptions here: a network/auth failure fetching the fallback must abort
    the whole run, not silently skip one flow)."""
    local_id = integration_ids.get(celigo_integration_id) if celigo_integration_id else None
    if local_id is not None:
        return local_id
    if not celigo_integration_id:
        return None
    fetched = await get_resource("integration", celigo_integration_id, token=token, region=region, client=http)
    local_id = await upsert_integration(db, tenant_id=tenant_id, connection_id=connection_id, sanitized=fetched)
    integration_ids[celigo_integration_id] = local_id
    return local_id


async def _record_attachments(
    db: AsyncSession,
    *,
    tenant_id,
    connection_id,
    flow_id: uuid.UUID,
    flow_step_id: uuid.UUID | None,
    refs: list[ScriptRef],
    script_ids: dict[str, uuid.UUID],
) -> int:
    """Upsert one `celigo_script_attachments` row per ref, resolving
    `script_id` from this run's own Phase C map when available (`None` when
    not -- script sync can lag flow sync; `upsert_script_attachment_from_ref`
    tolerates that by design, per its own docstring). Returns the count
    upserted."""
    for ref in refs:
        await upsert_script_attachment_from_ref(
            db,
            tenant_id=tenant_id,
            connection_id=connection_id,
            flow_id=flow_id,
            flow_step_id=flow_step_id,
            ref=ref,
            script_id=script_ids.get(ref.script_id),
        )
    return len(refs)


def _extract_provenance(obj: dict) -> tuple[str | None, str | None, str | None]:
    """`(record_type, operation, search_id)` from whichever of `netsuite_da`
    (imports) or `netsuite` (exports) *obj* carries -- Task 11's provenance
    input (fix round 2). observed-shapes.md: "imports carry netsuite_da;
    exports carry netsuite -- DIFFERENT KEY FROM IMPORTS", so this reads
    BOTH and keys on which one is actually PRESENT (never on which fetch
    loop the caller happened to be in -- same "presence decides, not the
    container name" discipline already used by `graph.walk_script_refs` and
    the sanitizer's `filter`/`transform` handling). An object with neither
    key (a non-NetSuite export/import) returns all three as `None` -- no
    guessing."""
    netsuite_da = obj.get("netsuite_da") or {}
    if netsuite_da:
        return netsuite_da.get("recordType"), netsuite_da.get("operation"), None
    restlet = (obj.get("netsuite") or {}).get("restlet") or {}
    if restlet:
        return restlet.get("recordType"), None, restlet.get("searchId")
    return None, None, None


async def _process_reference_object(
    db: AsyncSession,
    *,
    tenant_id,
    connection_id,
    obj: dict,
    referencing_flow_steps: dict[uuid.UUID, uuid.UUID] | None,
    script_ids: dict[str, uuid.UUID],
) -> tuple[int, int]:
    """One export/import object (Phase D): backfill EVERY matching
    `celigo_flow_steps` row, then record its script refs against every flow
    that references it.

    ONE-EXPORT-MANY-FLOWS MODELLING (the part the team lead flagged as most
    likely to be wrong -- spelled out here, not left implicit): the SAME
    export/import object is walked ONCE (`walk_script_refs` runs a single
    time, not once per flow -- the object's content doesn't change per
    flow), but a `celigo_script_attachments` row is upserted ONCE PER FLOW
    that references it (`celigo_script_attachments.flow_id` is NOT NULL, so
    "no flow" is unrepresentable -- an attachment must belong to exactly one
    flow row). If flow A and flow B both reference this export, this
    produces TWO attachment rows with the SAME `json_path`/`script_celigo_
    id` but DIFFERENT `flow_id` -- the unique key is `(tenant_id, flow_id,
    json_path)`, so these are legitimately two distinct rows, not a
    duplicate-of-one. `flow_step_id` is set to whichever step THAT flow's
    entry in *referencing_flow_steps* names (Phase B's "first step per flow
    wins" policy -- see `sync_flow_map_for_connection`) -- best-effort, per
    the model's own docstring, never re-derived from `json_path`.

    Returns `(rows_backfilled, attachments_upserted)`. `rows_backfilled`
    comes straight from the repository call's own rowcount, NOT from
    `len(referencing_flow_steps)` -- that map holds only the FIRST step seen
    per flow (deliberately, for the attachment association below), so if one
    flow references this object through more than one branch, the real
    UPDATE touches more rows than the map has entries for. Undercounting the
    summary would be a silent (if harmless) reporting bug, not a data bug --
    still worth getting right. `referencing_flow_steps` being `None`/empty
    means no currently-synced step references this object -- nothing to
    backfill (there is no row to update) and nothing to attach to (no flow
    to satisfy the NOT NULL column) -- the caller counts this as skipped,
    not fatal."""
    celigo_id = obj.get("_id")
    if not celigo_id or not referencing_flow_steps:
        return 0, 0

    record_type, operation, search_id = _extract_provenance(obj)
    rows_backfilled = await backfill_flow_step_reference_info(
        db,
        tenant_id=tenant_id,
        connection_id=connection_id,
        celigo_id=celigo_id,
        adaptor_type=obj.get("adaptorType"),
        connection_celigo_id=obj.get("_connectionId"),
        record_type=record_type,
        operation=operation,
        search_id=search_id,
    )

    refs = walk_script_refs(obj)
    if not refs:
        return rows_backfilled, 0

    attached = 0
    for flow_id, flow_step_id in referencing_flow_steps.items():
        attached += await _record_attachments(
            db,
            tenant_id=tenant_id,
            connection_id=connection_id,
            flow_id=flow_id,
            flow_step_id=flow_step_id,
            refs=refs,
            script_ids=script_ids,
        )
    return rows_backfilled, attached


async def _purge_expired_errors(db: AsyncSession, *, tenant_id, connection_id) -> int:
    """Mark every error whose OWN `purge_at` has passed (wall-clock `now()`)
    as purged -- independent of `resolved_at`, independent of any single
    step's sync (repository.mark_flow_errors_purged's own docstring: "the
    caller decides this, typically by comparing against each error's own
    purge_at"). Task 6 deliberately left this undone (an orchestration
    concern, not a per-step one); this is where it lands. NEVER deletes a
    row -- `repository.mark_flow_errors_purged`'s own guarantee."""
    expired_ids = (
        (
            await db.execute(
                select(CeligoFlowError.celigo_id).where(
                    CeligoFlowError.tenant_id == tenant_id,
                    CeligoFlowError.celigo_connection_id == connection_id,
                    CeligoFlowError.purge_at.is_not(None),
                    CeligoFlowError.purge_at <= datetime.now(timezone.utc),
                    CeligoFlowError.purged_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return await mark_flow_errors_purged(db, tenant_id=tenant_id, connection_id=connection_id, celigo_ids=expired_ids)


async def sync_flow_map_for_connection(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    token: str,
    region: str = "us",
    http_client: httpx.AsyncClient | None = None,
) -> SyncSummary:
    """Full flow-map sync for one Celigo connection: integrations -> flows ->
    steps -> scripts -> exports/imports (attachments + step backfill) ->
    errors per step, plus drift detection and purge marking. See module
    docstring for the phase-by-phase design and the all-or-nothing failure
    posture. Never commits -- the caller (`app/workers/tasks/
    celigo_flow_map_sync.py`) owns the transaction boundary and the
    freshness cursor, both only reached if this function returns without
    raising."""
    summary = SyncSummary()
    owns_client = http_client is None
    http = http_client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
    try:
        # Phase A -- integrations.
        integration_ids: dict[str, uuid.UUID] = {}
        async for integration in list_resource("integration", token=token, region=region, client=http):
            celigo_id = integration.get("_id")
            if not celigo_id:
                continue
            local_id = await upsert_integration(
                db, tenant_id=tenant_id, connection_id=connection_id, sanitized=integration
            )
            integration_ids[celigo_id] = local_id
            summary.integrations_synced += 1

        # Phase B -- flows, and each flow's own steps.
        pending_step_refs: list[tuple[str, str, _StepRef]] = []
        # Populated across Phases B/C/D, consumed in Phase D:
        #   script_ids: script's own celigo_id -> local id (Phase C).
        #   export_import_flow_steps: an export/import's celigo_id -> {flow_local_id: first
        #     step_local_id in that flow referencing it} -- built in Phase B's step loop, read
        #     in Phase D. "First step per flow wins" (a deterministic, documented choice, not a
        #     guess) is enough: flow_step_id on an attachment row is best-effort per the model's
        #     own docstring, and the export/import's own json_path doesn't distinguish branches.
        script_ids: dict[str, uuid.UUID] = {}
        export_import_flow_steps: dict[str, dict[uuid.UUID, uuid.UUID]] = defaultdict(dict)
        async for flow in list_resource("flow", token=token, region=region, client=http):
            flow_celigo_id = flow.get("_id")
            if not flow_celigo_id:
                continue

            integration_local_id = await _resolve_integration_id(
                db,
                tenant_id=tenant_id,
                connection_id=connection_id,
                integration_ids=integration_ids,
                celigo_integration_id=flow.get("_integrationId"),
                token=token,
                region=region,
                http=http,
            )
            if integration_local_id is None:
                summary.flows_skipped_no_integration += 1
                continue

            existing_flow = await _get_existing_flow(
                db, tenant_id=tenant_id, connection_id=connection_id, celigo_id=flow_celigo_id
            )
            flow_changes = _flow_drift(existing_flow, flow)

            flow_local_id = await upsert_flow(
                db,
                tenant_id=tenant_id,
                connection_id=connection_id,
                integration_id=integration_local_id,
                sanitized=flow,
            )
            summary.flows_synced += 1

            if flow_changes:
                await _record_drift(
                    db,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    object_kind="flow",
                    object_id=flow_local_id,
                    celigo_id=flow_celigo_id,
                    flow_id=flow_local_id,
                    changes=flow_changes,
                )
                summary.config_changes_recorded += len(flow_changes)

            # The flow object itself is a script-attachment site too --
            # routers can carry `script` (module docstring). flow_step_id is
            # always None here: a router-level ref belongs to the router,
            # not to any one step (celigo_script_attachments' own model
            # docstring).
            summary.attachments_synced += await _record_attachments(
                db,
                tenant_id=tenant_id,
                connection_id=connection_id,
                flow_id=flow_local_id,
                flow_step_id=None,
                refs=walk_script_refs(flow),
                script_ids=script_ids,
            )

            for step_input in extract_flow_steps(flow):
                branch_key = step_input.branch_id or "$root"
                existing_step = await _get_existing_step(
                    db,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    flow_id=flow_local_id,
                    celigo_id=step_input.celigo_id,
                    branch_key=branch_key,
                )
                step_changes = _step_drift(existing_step, step_input)

                step_local_id = await upsert_flow_step(
                    db,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    flow_id=flow_local_id,
                    step=step_input,
                )
                summary.steps_synced += 1

                if step_changes:
                    await _record_drift(
                        db,
                        tenant_id=tenant_id,
                        connection_id=connection_id,
                        object_kind="flow_step",
                        object_id=step_local_id,
                        celigo_id=step_input.celigo_id,
                        flow_id=flow_local_id,
                        changes=step_changes,
                    )
                    summary.config_changes_recorded += len(step_changes)

                pending_step_refs.append(
                    (flow_celigo_id, step_input.celigo_id, _StepRef(id=step_local_id, flow_id=flow_local_id))
                )
                # "First step per flow wins" -- see the map's own comment above.
                export_import_flow_steps[step_input.celigo_id].setdefault(flow_local_id, step_local_id)

        # Phase C -- scripts, independent of flow order.
        async for script in list_resource("script", token=token, region=region, client=http):
            celigo_id = script.get("_id")
            if not celigo_id:
                continue
            existing_script = await _get_existing_script(
                db, tenant_id=tenant_id, connection_id=connection_id, celigo_id=celigo_id
            )
            new_hash = _hash_content(script.get("content"))
            script_changes = _script_drift(existing_script, new_hash)

            script_local_id = await upsert_script(
                db, tenant_id=tenant_id, connection_id=connection_id, sanitized=script
            )
            script_ids[celigo_id] = script_local_id
            summary.scripts_synced += 1

            if script_changes:
                await _record_drift(
                    db,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    object_kind="script",
                    object_id=script_local_id,
                    celigo_id=celigo_id,
                    flow_id=None,
                    changes=script_changes,
                )
                summary.config_changes_recorded += len(script_changes)

        # Phase D -- exports and imports: backfill celigo_flow_steps.adaptor_type/
        # connection_celigo_id, and record script attachments per referencing flow.
        # See module docstring's Phase D entry for why this exists.
        for kind in _REFERENCE_OBJECT_KINDS:
            async for obj in list_resource(kind, token=token, region=region, client=http):
                celigo_id = obj.get("_id")
                referencing_flow_steps = export_import_flow_steps.get(celigo_id) if celigo_id else None
                if not referencing_flow_steps:
                    summary.exports_imports_skipped_no_flow += 1
                    continue
                rows_backfilled, attached = await _process_reference_object(
                    db,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    obj=obj,
                    referencing_flow_steps=referencing_flow_steps,
                    script_ids=script_ids,
                )
                summary.exports_imports_synced += 1
                summary.flow_steps_backfilled += rows_backfilled
                summary.attachments_synced += attached

        # Phase E -- errors, per step, in the order steps were synced. Only
        # reachable once every step above has a REAL celigo_flow_steps row.
        for flow_celigo_id, step_celigo_id, step_ref in pending_step_refs:
            raw_errors = await list_flow_errors_for_step(
                flow_celigo_id, step_celigo_id, token=token, region=region, client=http
            )
            summary.steps_with_errors_checked += 1
            summary.errors_snapshotted += len(raw_errors)
            await upsert_errors(
                db, tenant_id=tenant_id, connection_id=connection_id, step=step_ref, raw_errors=raw_errors
            )

        # Purge marking -- last, once per connection, independent of any
        # single step's sync.
        summary.errors_purged = await _purge_expired_errors(db, tenant_id=tenant_id, connection_id=connection_id)

        return summary
    finally:
        if owns_client:
            await http.aclose()
