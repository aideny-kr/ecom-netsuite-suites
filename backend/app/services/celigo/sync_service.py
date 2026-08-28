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
step"):
  Phase A: every integration (`client.list_resource("integration")`).
  Phase B: every flow (`client.list_resource("flow")`); for EACH flow, its
    steps are extracted (`repository.extract_flow_steps`) and upserted
    immediately after the flow itself. Flows and their own steps cannot be
    two fully separate phases -- Celigo has no "list steps" endpoint; a step
    only exists inside its flow's own payload -- so read this as one combined
    "flows -> steps" phase, per-flow.
  Phase C: every script (`client.list_resource("script")`), independent of
    flow order.
  Phase D: for every step collected during Phase B, in that same order, its
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

WHAT THIS DELIBERATELY DOES NOT DO:
  * Script attachments (`celigo_script_attachments` / `graph.walk_script_
    refs`) are NOT synced here. The brief's Step 2 lists five sequencing
    stages (integrations, flows, steps, scripts, errors); attachments is not
    a sixth. Task 5 built the storage layer
    (`repository.upsert_script_attachment_from_ref`) and Task 2 built the
    pure walker, but nothing wires them into a fetch loop anywhere in this
    branch -- `celigo_script_attachments` stays empty after this task runs.
    Flagging this explicitly rather than silently expanding scope: resolving
    a ref's `json_path` to a specific `flow_step_id` (vs. leaving it NULL,
    which the model's own docstring anticipates for router-level refs) is not
    a solved problem anywhere in this branch, and inventing that mapping
    without a live-observed shape would repeat this session's core mistake
    (see observed-shapes.md's repeated "do not invent, verify first" lesson).
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
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.celigo import CeligoFlow, CeligoFlowError, CeligoFlowStep, CeligoScript
from app.services.celigo.client import get_resource, list_flow_errors_for_step, list_resource
from app.services.celigo.errors import upsert_errors
from app.services.celigo.repository import (
    extract_flow_steps,
    insert_config_change,
    mark_flow_errors_purged,
    upsert_flow,
    upsert_flow_step,
    upsert_integration,
    upsert_script,
)

_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=30.0)


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
    steps -> scripts -> errors per step, plus drift detection and purge
    marking. See module docstring for the phase-by-phase design and the
    all-or-nothing failure posture. Never commits -- the caller (`app/
    workers/tasks/celigo_flow_map_sync.py`) owns the transaction boundary and
    the freshness cursor, both only reached if this function returns without
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

        # Phase D -- errors, per step, in the order steps were synced. Only
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
