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
  Phase C: every script -- LISTED (`client.list_resource("script")`) and
    then FETCHED BY ID (`client.get_resource("script", id)`), because the
    list omits `content` and the single GET omits `_sourceId`. Only
    `content` is taken from the GET; the list item is the record. A GET
    without a body is counted (`scripts_without_content`) and never
    overwrites stored content. Independent of flow order. (Until
    2026-09-02 this phase listed only, and every stored script was empty.)
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
  Phase E (REWRITTEN, VERIFIED LIVE 2026-09-03 -- read this before the
    "WHAT THIS DELIBERATELY STILL DOES NOT DO" section below, which used to
    describe a `_stepId` design this phase no longer has): for every FLOW
    that had steps collected during Phase B, in first-seen flow order, its
    per-flow error SUMMARY is fetched first (`client.list_flow_error_
    summary` -- `GET /v1/flows/{flow_id}/errors`, no query params, returns
    `{"flowErrors": [{"_expOrImpId", "numError"}, ...]}`, one entry per
    export/import in the flow). Each of that flow's steps, in step order, is
    then handled from the summary alone:
      * absent from the summary -- neither fetched nor recorded
        (`steps_not_in_error_summary`). Absence is not evidence of anything.
      * a verified `numError == 0` -- resolved via `errors.upsert_errors(
        raw_errors=[], raw_errors_is_complete=True)` WITHOUT a per-step
        fetch (`steps_skipped_zero_errors`).
      * a non-zero count -- only NOW is `client.list_flow_errors_for_step`
        (`GET /v1/flows/{flow_id}/{resourceId}/errors`) called for that one
        step's actual open errors, snapshotted the same way this phase
        always has (`errors.upsert_errors`; `upsert_errors` needs a real
        `step.id`/`step.flow_id`, which is why this is its own pass after
        every step already exists as a real `celigo_flow_steps` row -- see
        `_StepRef` below for how).
    Three guards (independent-model review, 2026-09-03): a step whose
    export/import an EARLIER step of the same flow already referenced is
    skipped (`steps_sharing_resource` -- errors are per resource, the first
    step owns them); a non-zero count whose per-resource listing came back
    EMPTY is recorded as incomplete and resolves nothing
    (`steps_with_inconsistent_errors` -- the endpoints disagree, and an
    empty listing is what the old bug looked like); and the flow's
    `errors_checked_at` cursor (`repository.mark_flow_errors_checked`) is
    stamped ONLY when every step reached a verdict this run -- otherwise it
    is left as it was (`flows_errors_unverified`), so a never-verified flow
    keeps NULL and renders as "errors not checked yet", never a green zero.
    THE BUG THIS REPLACED: the pre-2026-09-03 version of this phase
    called `list_flow_errors_for_step` for every step UNCONDITIONALLY, with
    a `?_stepId=<step's own celigo_id>` query param on `GET /v1/flows/
    {flow_id}/errors` -- an endpoint that, verified live, IGNORES `_stepId`
    entirely and returns the summary shape regardless. Reading
    `body["errors"]` off a summary body (which has no such key) produced
    `[]` every time, and `upsert_errors(raw_errors=[],
    raw_errors_is_complete=True)` then resolved every previously-open error
    as if Celigo had reported none. Every run "succeeded", the freshness
    cursor advanced, and not one real error ever landed -- 112 open errors
    across 16 production flows, invisible on staging, is what this fixes.
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
  * `list_flow_error_summary`'s counts are read as `int(numError)` and used
    only to decide "fetch / resolve-as-zero / leave alone" for each step --
    this module never stores a summary count itself, and never treats it as
    a substitute for `celigo_flow_errors`' own rows (the audit trail is still
    built exclusively from `list_flow_errors_for_step`'s per-resource
    fetches, or from a verified zero).
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.celigo import CeligoFlow, CeligoFlowError, CeligoFlowStep, CeligoScript
from app.services.celigo.client import (
    CeligoIncompleteListingError,
    CeligoNotFoundError,
    get_resource,
    list_flow_error_summary,
    list_flow_errors_for_step,
    list_resource,
)
from app.services.celigo.errors import upsert_errors
from app.services.celigo.graph import ScriptRef, walk_script_refs
from app.services.celigo.repository import (
    backfill_attachment_script_ids,
    backfill_flow_step_reference_info,
    extract_flow_steps,
    insert_config_change,
    mark_flow_errors_checked,
    mark_flow_errors_purged,
    purge_sandbox_rows,
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
    # PRODUCTION ONLY (operator directive 2026-09-01: "don't bring sandbox
    # celigo, just production"). Sandbox integrations are skipped in Phase A,
    # their flows in Phase B, and rows synced before this rule are purged --
    # three counts so a run's summary says what happened to the sandbox half
    # of the account (19 of 36 integrations, 118 of 239 flows on the live one).
    integrations_skipped_sandbox: int = 0
    integrations_purged_sandbox: int = 0
    flows_synced: int = 0
    flows_skipped_sandbox: int = 0
    flows_skipped_no_integration: int = 0
    steps_synced: int = 0
    scripts_synced: int = 0
    scripts_skipped_sandbox: int = 0
    scripts_purged_sandbox: int = 0
    # A per-id GET that answered without a body. The stored content is kept
    # (repository.upsert_script never overwrites it with NULL); this count is
    # how a run says so instead of pretending every script has source.
    scripts_without_content: int = 0
    exports_imports_synced: int = 0
    exports_imports_skipped_sandbox: int = 0
    exports_imports_skipped_no_flow: int = 0
    flow_steps_backfilled: int = 0
    attachments_synced: int = 0
    # FIX (whole-branch review finding 6): rows Phase B wrote with script_id
    # NULL, later filled in once Phase C's script_ids map existed.
    attachment_script_ids_backfilled: int = 0
    steps_with_errors_checked: int = 0
    errors_snapshotted: int = 0
    # FIX ROUND 9 (re-review R1b): steps whose error listing came back
    # TRUNCATED. Non-zero means this run is partial -- those steps' errors
    # were recorded but nothing was resolved from them (see Phase E). It is
    # how a partial run stays visible now that one truncated step no longer
    # aborts the whole sync.
    steps_with_incomplete_errors: int = 0
    # VERIFIED LIVE 2026-09-03: Phase E now gates on each flow's error
    # SUMMARY (`list_flow_error_summary`) before deciding what to do with a
    # step, rather than fetching every step unconditionally. These three
    # counters say what the gate decided:
    #   flows_errors_checked -- flows whose summary was fetched and whose
    #     errors_checked_at cursor was therefore stamped this run.
    steps_skipped_zero_errors: int = 0
    # steps_skipped_zero_errors -- the summary reported a real ZERO for this
    #   step: resolved via upsert_errors(raw_errors=[]) WITHOUT a per-step
    #   fetch (a verified zero, not an absence).
    steps_not_in_error_summary: int = 0
    # steps_not_in_error_summary -- this step's id never appeared in its
    #   flow's summary at all: neither fetched nor resolved, since absence
    #   from a summary is not evidence anything is open OR resolved.
    flows_errors_checked: int = 0
    # flows_errors_unverified -- flows whose summary WAS consulted but at
    #   least one step reached no verdict (absent from the summary, an
    #   empty listing behind a non-zero count, or a truncated listing). Their
    #   errors_checked_at is left exactly as it was: the stamp means "every
    #   step verified as of", and an unverified run has not earned it.
    flows_errors_unverified: int = 0
    # steps_with_inconsistent_errors -- the summary reported a NON-ZERO count
    #   but the per-resource listing came back empty. The two endpoints
    #   disagree; an empty `errors[]` is exactly what the pre-2026-09-03 bug
    #   looked like, so nothing is resolved from it (recorded as incomplete).
    steps_with_inconsistent_errors: int = 0
    # steps_sharing_resource -- a step referencing an export/import that an
    #   earlier step of the SAME flow already referenced (Celigo lets two
    #   router branches reuse one resource). Errors are per resource, so the
    #   first step owns them; the duplicate is neither fetched nor upserted,
    #   which is what stops each duplicate re-parenting the same error rows.
    steps_sharing_resource: int = 0
    # summary_errors_without_step -- the flow's summary reported a NON-ZERO
    #   count on a resource this run has no step row for (Phase B skipped or
    #   never saw it). Nothing can be attached, so nothing is fetched -- but
    #   the flow is left unverified: its local zero is not Celigo's number.
    summary_errors_without_step: int = 0
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


class _FlowSkip(Enum):
    """Why `_resolve_integration_id` could not give a flow a local integration
    id. Two reasons, two summary counters -- a sandbox skip is deliberate; a
    missing integration is a listing gap worth noticing."""

    SANDBOX = "sandbox"
    NO_INTEGRATION = "no_integration"


def _is_sandbox(obj: dict) -> bool:
    """PRODUCTION ONLY's one classifier, applied at the ingestion boundary of
    EVERY kind the sync reads (integration, script, export, import -- flows
    carry no flag of their own and follow their integration). One function
    so a kind cannot be forgotten: the first cut of PR #216 checked
    integrations inline and left scripts (132 of 259 on the live account)
    and exports/imports unchecked.

    `is True`, never truthiness: an absent flag is production. Hiding on a
    missing field would let a sanitizer or API change silently erase real
    objects. Read-side twin: `app.models.celigo.celigo_integration_is_
    production` / `celigo_script_is_production` (`sandbox IS NOT TRUE`)."""
    return obj.get("sandbox") is True


async def _list_production(
    kind: str,
    *,
    token: str,
    region: str,
    http: httpx.AsyncClient,
    summary: SyncSummary,
    skipped_field: str,
    skipped_ids: set[str] | None = None,
):
    """THE seam every listed object enters the sync through: `list_resource`
    with `_is_sandbox` applied. A kind cannot be iterated in this module any
    other way, so a kind cannot be forgotten -- the first cut of PR #216
    checked integrations inline and missed scripts; round 3's gate found the
    remaining four inline checks were the shape that kept producing majors.

    A skipped object is counted on `summary.<skipped_field>` and, when the
    caller needs to recognise it later (Phase B skipping a sandbox
    integration's flows; the end-of-run purge catching a row whose stored
    flag has gone stale), its Celigo id is added to *skipped_ids*."""
    async for obj in list_resource(kind, token=token, region=region, client=http):
        if _is_sandbox(obj):
            celigo_id = obj.get("_id")
            if skipped_ids is not None and celigo_id:
                skipped_ids.add(celigo_id)
            setattr(summary, skipped_field, getattr(summary, skipped_field) + 1)
            continue
        yield obj


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
    sandbox_integration_ids: set[str],
    summary: SyncSummary,
    celigo_integration_id: str | None,
    token: str,
    region: str,
    http: httpx.AsyncClient,
) -> uuid.UUID | _FlowSkip:
    """Local id for `celigo_integration_id`, from this run's own Phase A map
    when present. Falls back to an on-demand `get_resource` + upsert for the
    rare case a flow references an integration Phase A's listing didn't
    return -- never silently drop the flow over a listing gap.

    Returns a `_FlowSkip` reason instead of an id in two cases, named
    explicitly rather than signalled by mutating a set the caller then has
    to re-read (gate round 2): `NO_INTEGRATION` when `celigo_integration_id`
    itself is falsy (a malformed flow, no id to even try); `SANDBOX` when the
    integration is a sandbox one -- already in `sandbox_integration_ids`
    from Phase A, or discovered to be one by the fallback fetch, which then
    records it there AND counts it in `summary.integrations_skipped_sandbox`
    exactly as Phase A would have. Without that second check the fallback
    would fetch-and-upsert the very sandbox integration Phase A just
    refused, one flow at a time.

    A genuine fetch failure for a REAL id propagates uncaught, same as
    everything else in this module (no swallowed exceptions here: a
    network/auth failure fetching the fallback must abort the whole run, not
    silently skip one flow)."""
    if not celigo_integration_id:
        return _FlowSkip.NO_INTEGRATION
    if celigo_integration_id in sandbox_integration_ids:
        return _FlowSkip.SANDBOX
    local_id = integration_ids.get(celigo_integration_id)
    if local_id is not None:
        return local_id
    fetched = await get_resource("integration", celigo_integration_id, token=token, region=region, client=http)
    if _is_sandbox(fetched):
        sandbox_integration_ids.add(celigo_integration_id)
        summary.integrations_skipped_sandbox += 1
        return _FlowSkip.SANDBOX
    local_id = await upsert_integration(db, tenant_id=tenant_id, connection_id=connection_id, sanitized=fetched)
    integration_ids[celigo_integration_id] = local_id
    # Written this run, same as a Phase A upsert -- the summary counts what
    # was WRITTEN, and a listing-gap fallback is a write (gate round 4).
    summary.integrations_synced += 1
    return local_id


async def _record_attachments(
    db: AsyncSession,
    *,
    tenant_id,
    connection_id,
    flow_id: uuid.UUID,
    flow_step_id: uuid.UUID | None,
    refs: list[ScriptRef],
    reference_object_celigo_id: str | None,
    script_ids: dict[str, uuid.UUID],
) -> int:
    """Upsert one `celigo_script_attachments` row per ref, resolving
    `script_id` from this run's own Phase C map when available (`None` when
    not -- script sync can lag flow sync; `upsert_script_attachment_from_ref`
    tolerates that by design, per its own docstring). Returns the count
    upserted.

    `reference_object_celigo_id` is the celigo id of the object these refs
    were WALKED FROM -- `None` for the flow object itself (Phase B), the
    export/import's own id in Phase D. It is passed straight through to the
    repository, which owns what it means for row identity; see
    `repository.qualify_json_path`."""
    for ref in refs:
        await upsert_script_attachment_from_ref(
            db,
            tenant_id=tenant_id,
            connection_id=connection_id,
            flow_id=flow_id,
            flow_step_id=flow_step_id,
            ref=ref,
            reference_object_celigo_id=reference_object_celigo_id,
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

    MANY-EXPORTS-ONE-FLOW, the case the paragraph above did NOT consider
    (whole-branch review finding 1): this function is called once per
    export/import, so two objects in the SAME flow that each carry a script
    at `transform.script` used to write the same `(flow_id, json_path)` --
    the second silently overwrote the first, `script_celigo_id` included.
    Multi-step NetSuite flows are the ordinary case. `*obj*`'s own celigo id
    is therefore passed to `_record_attachments` as
    `reference_object_celigo_id`, and the repository qualifies the path with
    it; see `repository.qualify_json_path` for why that beat widening the
    unique key.

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
    raw_name = obj.get("name")
    reference_name = raw_name if isinstance(raw_name, str) and raw_name.strip() else None
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
        reference_name=reference_name,
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
            reference_object_celigo_id=celigo_id,
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
        # Phase A -- integrations. PRODUCTION ONLY (`_is_sandbox`): a sandbox
        # integration is remembered -- so Phase B can skip its flows without
        # the listing-gap fallback re-fetching it, and so the end-of-run
        # purge can remove a stored row whose flag has gone stale -- and is
        # never written. Same for scripts in Phase C (`sandbox_script_ids`).
        integration_ids: dict[str, uuid.UUID] = {}
        sandbox_integration_ids: set[str] = set()
        sandbox_script_ids: set[str] = set()
        async for integration in _list_production(
            "integration",
            token=token,
            region=region,
            http=http,
            summary=summary,
            skipped_field="integrations_skipped_sandbox",
            skipped_ids=sandbox_integration_ids,
        ):
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
        async for flow in _list_production(
            "flow", token=token, region=region, http=http, summary=summary, skipped_field="flows_skipped_sandbox"
        ):
            flow_celigo_id = flow.get("_id")
            if not flow_celigo_id:
                continue

            resolved = await _resolve_integration_id(
                db,
                tenant_id=tenant_id,
                connection_id=connection_id,
                integration_ids=integration_ids,
                sandbox_integration_ids=sandbox_integration_ids,
                summary=summary,
                celigo_integration_id=flow.get("_integrationId"),
                token=token,
                region=region,
                http=http,
            )
            if isinstance(resolved, _FlowSkip):
                if resolved is _FlowSkip.SANDBOX:
                    summary.flows_skipped_sandbox += 1
                else:
                    summary.flows_skipped_no_integration += 1
                continue
            integration_local_id = resolved

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
                # The flow object IS the root these paths are relative to --
                # nothing to qualify them with, and qualifying would make a
                # path depend on where the walk started.
                reference_object_celigo_id=None,
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
        async for script in _list_production(
            "script",
            token=token,
            region=region,
            http=http,
            summary=summary,
            skipped_field="scripts_skipped_sandbox",
            skipped_ids=sandbox_script_ids,
        ):
            celigo_id = script.get("_id")
            if not celigo_id:
                continue
            # Celigo's LIST omits `content` for every script (probed live,
            # 2026-09-02: 0 of 261 carried it; the 2026-08-17 spec said so and
            # this phase listed anyway -- 129 empty rows in production, and a
            # viewer that said "No source recorded" for all of them). Only the
            # per-id GET returns the body. ONLY `content` is taken from it: the
            # list item is the record (it decided sandbox routing, and it is
            # the one carrying `_sourceId`, the clone-family key, which the
            # single GET lacks). A GET that answers without a body is counted
            # and changes nothing -- `upsert_script` keeps stored content when
            # the payload has none, so this path can never re-empty a script
            # (PR #217 gate). One extra call per PRODUCTION script; sandbox
            # ones never reach this line. `get_resource` sanitizes.
            try:
                fetched = await get_resource("script", celigo_id, token=token, region=region, client=http)
            except CeligoNotFoundError:
                # The script was deleted in the seconds between the LIST and
                # this GET. One object, self-healing next run (the list will
                # not name it again) -- the same narrowing Phase E makes for
                # one step's truncated error listing (gate round 3). Counted
                # as body-less; stored content is kept. Auth, network and
                # upstream 5xx still abort the run, as the module rule says.
                fetched = {}
            fetched_content = fetched.get("content")
            if isinstance(fetched_content, str):
                # An EMPTY string is a body too -- someone cleared the script
                # in Celigo, and that edit must land and be hashed. Only an
                # ABSENT key is the no-body case (gate round 2). The body and
                # its timestamp come from the same object.
                script = {**script, "content": fetched_content}
                if "lastModified" in fetched:
                    script["lastModified"] = fetched["lastModified"]
            else:
                summary.scripts_without_content += 1
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

        # Phase B's flow/router-level attachments (`_record_attachments` for
        # the flow object itself, above) resolved `script_id` from
        # `script_ids` while it was still empty -- Phase C, which actually
        # populates it, had not run yet. FIX (whole-branch review finding 6):
        # go back now that it has and fill in whatever Phase B left NULL.
        # Phase D's own attachments never had this problem (Phase D runs
        # after Phase C), so this call is scoped to what Phase B could not
        # have resolved -- see `backfill_attachment_script_ids`'s own
        # docstring for why this is safe to run unconditionally here.
        summary.attachment_script_ids_backfilled = await backfill_attachment_script_ids(
            db, tenant_id=tenant_id, connection_id=connection_id, script_ids=script_ids
        )

        # Phase D -- exports and imports: backfill celigo_flow_steps.adaptor_type/
        # connection_celigo_id, and record script attachments per referencing flow.
        # See module docstring's Phase D entry for why this exists.
        for kind in _REFERENCE_OBJECT_KINDS:
            async for obj in _list_production(
                kind,
                token=token,
                region=region,
                http=http,
                summary=summary,
                skipped_field="exports_imports_skipped_sandbox",
            ):
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

        # Phase E -- errors, per FLOW (VERIFIED LIVE 2026-09-03; see module
        # docstring's Phase E entry for the full story of why this replaced
        # an unconditional per-step fetch). Steps are grouped by their flow,
        # preserving first-seen flow order and, within a flow, step order --
        # `pending_step_refs` was already built in exactly that order by
        # Phase B, so a single pass over it is enough.
        steps_by_flow: dict[str, list[tuple[str, _StepRef]]] = defaultdict(list)
        for flow_celigo_id, step_celigo_id, step_ref in pending_step_refs:
            steps_by_flow[flow_celigo_id].append((step_celigo_id, step_ref))

        for flow_celigo_id, steps in steps_by_flow.items():
            # A `CeligoError` here (auth rejected, an unparseable body, a
            # 5xx) propagates and aborts the whole run -- exactly like
            # Phases A-D. Only ONE step's own listing
            # (`CeligoIncompleteListingError`, below) is ever contained to
            # just that step; a failure to even get the flow's summary says
            # nothing about any one step in particular.
            counts = await list_flow_error_summary(flow_celigo_id, token=token, region=region, client=http)
            flow_local_id = steps[0][1].flow_id
            # Every step must reach a verdict this run for the flow's
            # errors_checked_at to advance -- see `SyncSummary.
            # flows_errors_unverified` for the three ways one fails to.
            flow_verified = True
            # Resources already fetched for THIS flow. Errors are per
            # export/import, not per step, and `celigo_flow_errors` is unique
            # by error id -- a second step referencing the same resource
            # would fetch the same rows again and re-parent them to itself.
            seen_resources: set[str] = set()

            for step_celigo_id, step_ref in steps:
                if step_celigo_id in seen_resources:
                    summary.steps_sharing_resource += 1
                    continue
                seen_resources.add(step_celigo_id)

                if step_celigo_id not in counts:
                    # This step's id never appeared in its flow's summary at
                    # all -- absence is not evidence anything resolved (or
                    # that anything is open), so nothing is fetched and
                    # nothing is recorded either way.
                    summary.steps_not_in_error_summary += 1
                    flow_verified = False
                    continue

                if counts[step_celigo_id] == 0:
                    # A VERIFIED zero from the summary -- resolve any
                    # previously-open error for this step WITHOUT a per-step
                    # fetch. `raw_errors_is_complete=True` is correct here:
                    # the summary IS this step's whole current listing, it
                    # just happens to be empty.
                    summary.steps_skipped_zero_errors += 1
                    summary.steps_with_errors_checked += 1
                    await upsert_errors(
                        db,
                        tenant_id=tenant_id,
                        connection_id=connection_id,
                        step=step_ref,
                        raw_errors=[],
                        raw_errors_is_complete=True,
                    )
                    continue

                # Non-zero -- fetch this step's actual open errors. FIX ROUND
                # 9 (re-review R1b): truncation is contained to the ONE step
                # it concerns. Before this, a single step exceeding
                # `_MAX_ERROR_PAGES` aborted the entire connection sync
                # (phases A-E) on every run until a human intervened -- and
                # the per-step escape hatch built for exactly this case had
                # no caller. Only `CeligoIncompleteListingError` is caught,
                # never the base `CeligoError`: a rejected token, an
                # unparseable body or a 5xx says nothing about one step in
                # particular and must still abort.
                try:
                    raw_errors = await list_flow_errors_for_step(
                        flow_celigo_id, step_celigo_id, token=token, region=region, client=http
                    )
                    # The fetcher raises rather than truncate, so a list that
                    # came back at all is this step's WHOLE current listing.
                    raw_errors_is_complete = True
                except CeligoIncompleteListingError as exc:
                    # Record what did arrive; resolve nothing. Absence from
                    # an admittedly-partial listing is not evidence an error
                    # is gone.
                    raw_errors = exc.partial_errors
                    raw_errors_is_complete = False
                    summary.steps_with_incomplete_errors += 1
                    flow_verified = False
                if raw_errors_is_complete and not raw_errors:
                    # The summary said "> 0", the per-resource listing said
                    # "none" (a 204, or a body without `errors[]`). The
                    # endpoints disagree, and an empty listing is exactly
                    # what the pre-2026-09-03 bug produced -- treat it as
                    # incomplete so nothing previously open gets resolved.
                    raw_errors_is_complete = False
                    summary.steps_with_inconsistent_errors += 1
                    flow_verified = False
                summary.steps_with_errors_checked += 1
                summary.errors_snapshotted += len(raw_errors)
                await upsert_errors(
                    db,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    step=step_ref,
                    raw_errors=raw_errors,
                    # Stated explicitly because `upsert_errors` has no
                    # default for this (FIX ROUND 9) -- see its docstring.
                    raw_errors_is_complete=raw_errors_is_complete,
                )

            # Errors Celigo reports on a resource this run has NO step for
            # cannot be attached anywhere -- and a flow whose local zero
            # hides them is not verified, whatever its own steps said.
            unowned = [rid for rid, n in counts.items() if n > 0 and rid not in seen_resources]
            if unowned:
                summary.summary_errors_without_step += len(unowned)
                flow_verified = False

            # Stamp the freshness cursor the data-status banner reads ONLY
            # when every step of this flow reached a verdict (a verified zero
            # or a complete non-empty listing) and nothing reported went
            # unattached. Otherwise the stamp stays exactly as it was -- a
            # flow never fully verified keeps NULL and renders as "errors not
            # checked yet", never as a green zero.
            if flow_verified:
                await mark_flow_errors_checked(
                    db, tenant_id=tenant_id, flow_id=flow_local_id, checked_at=datetime.now(timezone.utc)
                )
                summary.flows_errors_checked += 1
            else:
                summary.flows_errors_unverified += 1

        # Purge marking -- last, once per connection, independent of any
        # single step's sync.
        summary.errors_purged = await _purge_expired_errors(db, tenant_id=tenant_id, connection_id=connection_id)

        # PRODUCTION ONLY, the purge half -- last, once every phase has said
        # what it saw: rows flagged sandbox in the DB (written before the
        # rule existed: 19 integrations, 118 flows, 132 scripts on the live
        # account), plus rows whose Celigo object THIS run reported as
        # sandbox even though the stored flag still says production (an
        # integration flipped after an earlier sync -- gate finding, PR
        # #216). `purge_sandbox_rows` also deletes the flow-error rows the
        # FK would only SET NULL. Unconditional on purpose: the stored-flag
        # half has no in-run signal to gate on, and it is one indexed
        # statement per kind.
        summary.integrations_purged_sandbox, summary.scripts_purged_sandbox = await purge_sandbox_rows(
            db,
            tenant_id=tenant_id,
            connection_id=connection_id,
            integration_celigo_ids=sandbox_integration_ids,
            script_celigo_ids=sandbox_script_ids,
        )
        return summary
    finally:
        if owns_client:
            await http.aclose()
