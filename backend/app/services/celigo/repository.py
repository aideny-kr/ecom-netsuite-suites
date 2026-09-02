"""Idempotent repository for the Celigo flow-map tables (migration 094).

Read `app/models/celigo.py` and the migration's own docstring first -- this
module is the storage layer over those seven tables, nothing more. It does
NOT call `sanitizer.sanitize()` itself and does NOT fetch from Celigo -- both
belong to whichever task drives the sync loop (client.py's fetchers / a
future orchestrator). Every function here TRUSTS its caller passed an
already-sanitized payload (see sanitizer.py's module docstring: `include`/
`exclude` do not reliably strip payload fields on the wire, so sanitize() is
the ONLY effective control -- a raw Celigo object must never reach this
module). `raw_json` columns store the sanitized dict verbatim, unmodified, so
Task 7's drift-diff has something stable to compare against.

Flat async functions (`db: AsyncSession` first arg), matching this codebase's
established ingestion idiom (`app/services/ingestion/base.py`'s
`save_cursor_async`/`upsert_canonical`), not a class -- `client.py` (Task 3)
and `graph.py` (Task 2) are flat modules too.

THREE DESIGN POINTS THAT AREN'T OBVIOUS FROM THE FUNCTION SIGNATURES ALONE:

1. **`branch_key` (celigo_flow_steps) and `dedup_key` (celigo_scripts) are
   STORED GENERATED columns.** No function in this module ever includes
   either key in an INSERT/UPDATE values dict -- Postgres computes both from
   `branch_id`/`source_id` respectively. See `app/models/celigo.py`'s
   `Computed(...)` declarations for the second line of defence.

2. **`upsert_flow_step`'s unique key deliberately omits `role`** (migration
   094, deviation 1) -- if the same export/import id is ever claimed as BOTH
   a generator and a processor within one flow/branch, an unconditional
   `ON CONFLICT DO UPDATE` would silently overwrite one role's row with the
   other's, turning a real data anomaly into invisible corruption. Instead,
   the upsert's `DO UPDATE ... WHERE role = EXCLUDED.role` guard means a
   role-mismatched conflict updates ZERO rows and returns nothing from
   `RETURNING` -- which this module treats as a signal to raise
   `FlowStepRoleCollisionError` itself, explicitly, rather than letting a raw
   Postgres unique-violation happen (a plain unguarded `ON CONFLICT DO
   UPDATE` never raises one for this case in the first place; a plain INSERT
   with no conflict handling would raise on every legitimate re-sync, which
   fails the idempotency requirement). Either way, the collision surfaces as
   a loud, uncaught exception -- never swallowed, never silently applied.

3. **Errors are never deleted.** There is no delete-capable function for
   `celigo_flow_errors` anywhere in this module -- `mark_flow_errors_resolved`
   / `mark_flow_errors_purged` are the only state transitions, and both are
   plain `UPDATE`s. Surviving Celigo's ~30-day purge is the entire point of
   this table (design spec G2); a repository method that could remove a row
   here would defeat the feature. `test_celigo_repository.py`'s
   `test_repository_module_exposes_no_delete_function_for_flow_errors` pins
   this as a live guard, not just a comment.

4. **`upsert_error_signature`/`upsert_flow_error` require a REAL
   `connection_id`, even though `celigo_connection_id` is a nullable column
   on both tables.** Postgres treats NULL as DISTINCT for UNIQUE-constraint
   purposes -- the exact trap `dedup_key`/`branch_key` were introduced to
   solve for scripts/steps, and it re-enters here through the audit tables'
   SET NULL fix (migration deviation 8): once a connection is deleted and
   `celigo_connection_id` becomes NULL on the orphaned rows, `ON CONFLICT ON
   CONSTRAINT uq_celigo_error_signatures_identity` / `..._flow_errors_identity`
   NEVER fires for a subsequent call with `connection_id=None` (NULL is
   never equal to NULL), so it would silently INSERT a new row every time
   instead of updating -- unbounded duplication, not idempotent. The column
   only ever becomes NULL via the migration's own SET NULL delete trigger,
   an event outside any upsert caller's control and never something a
   sync-loop should be choosing to pass in deliberately. Requiring a real
   `connection_id` here removes the unsafe call shape by construction rather
   than guarding around it after the fact ("remove the possibility, not the
   instance") -- both functions also raise `ValueError` at runtime if `None`
   slips through anyway, since Python type hints aren't enforced. A row that
   has ALREADY been orphaned (via the DB's own SET NULL, not via this
   module) is intentionally left alone: it's the audit trail, and a later
   resync under a NEW connection creates its own fresh row rather than
   reaching back to touch the orphaned one -- see
   `test_orphaned_error_and_resync_under_a_new_connection_do_not_collide`.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
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
    celigo_script_is_production,
)
from app.services.celigo.graph import ScriptRef


class FlowStepRoleCollisionError(Exception):
    """Raised by `upsert_flow_step`/`sync_flow_steps` when the same
    `(flow_id, celigo_id, branch_key)` already exists with a DIFFERENT
    `role` than the one being written. See module docstring point 2 -- this
    must never be caught and swallowed by a caller."""

    def __init__(self, *, flow_id: uuid.UUID, celigo_id: str, branch_key_hint: str | None, new_role: str) -> None:
        self.flow_id = flow_id
        self.celigo_id = celigo_id
        self.new_role = new_role
        super().__init__(
            f"flow_id={flow_id} celigo_id={celigo_id!r} branch={branch_key_hint!r}: "
            f"already exists with a role different from {new_role!r} -- refusing to silently overwrite"
        )


def _set_clause(values: dict, *, exclude: set[str]) -> dict:
    """Build an ON CONFLICT DO UPDATE SET clause from *values*, excluding the
    identity columns in *exclude* (which must never change on a conflict) and
    always refreshing `updated_at`.

    `on_conflict_do_update(set_=...)` takes an explicit column->expression
    dict; unlike a normal ORM-managed UPDATE via session flush, a
    mapped_column's Python-side `onupdate=` callable (`TimestampMixin`) does
    NOT fire for columns left out of this dict -- so `updated_at` would stay
    frozen at its original INSERT value on every subsequent upsert unless
    it's named here explicitly.
    """
    out = {k: v for k, v in values.items() if k not in exclude}
    out["updated_at"] = func.now()
    return out


def _parse_celigo_timestamp(value: object) -> datetime | None:
    """Best-effort parse of a Celigo timestamp field into an aware datetime.

    Defensive by design: `celigo_last_modified`/`last_executed_at` etc. are
    cosmetic, non-identity fields (unlike `celigo_id`, which is never
    defaulted or guessed at) -- an unparseable or absent value degrades to
    NULL rather than aborting the whole upsert. NOT independently verified
    against a live response (observed-shapes.md lists `lastModified` as a
    present key but doesn't record its wire format); handles the two shapes
    most REST APIs of this kind use -- ISO-8601 strings and epoch
    milliseconds -- and returns None for anything else.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _content_hash(content: str | None) -> str | None:
    """Repository-owned hash of a script's `content`, never Celigo-sourced --
    guarantees `content_hash` always matches what's actually stored, rather
    than trusting a caller-supplied value that could drift out of sync."""
    if content is None:
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# celigo_integrations
# ---------------------------------------------------------------------------


async def upsert_integration(
    db: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, sanitized: dict
) -> uuid.UUID:
    """Upsert one `celigo_integrations` row from a SANITIZED integration
    payload (already through `sanitizer.sanitize("integration", raw)`)."""
    values = dict(
        tenant_id=tenant_id,
        celigo_connection_id=connection_id,
        celigo_id=sanitized["_id"],
        name=sanitized.get("name", ""),
        sandbox=sanitized.get("sandbox"),
        mode=sanitized.get("mode"),
        description=sanitized.get("description"),
        celigo_last_modified=_parse_celigo_timestamp(sanitized.get("lastModified")),
        raw_json=sanitized,
    )
    stmt = (
        insert(CeligoIntegration)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_celigo_integrations_identity",
            set_=_set_clause(values, exclude={"tenant_id", "celigo_connection_id", "celigo_id"}),
        )
        .returning(CeligoIntegration.id)
    )
    return (await db.execute(stmt)).scalar_one()


async def purge_sandbox_rows(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    integration_celigo_ids: Iterable[str],
    script_celigo_ids: Iterable[str],
) -> tuple[int, int]:
    """Remove every sandbox integration and sandbox script under one
    connection; returns `(integrations_purged, scripts_purged)`. The flow map
    is PRODUCTION ONLY (operator directive 2026-09-01); `sync_service.py`
    stops writing sandbox rows and calls this at the end of every run so the
    DB matches that promise.

    A row is sandbox if its STORED flag says so (rows written before the
    rule existed) OR if its Celigo id is in the set THIS run classified as
    sandbox. The second half is not redundant (PR #216 gate finding, major):
    the sync never upserts a sandbox object, so an integration that was
    production when first synced and flipped to sandbox since keeps
    `sandbox = false` in the DB forever -- a purge keyed on the stored flag
    alone would never see it.

    Cascade is NOT enough on its own. `celigo_flows.integration_id`, the
    flow's steps, script attachments and config changes are `ON DELETE
    CASCADE`, but `celigo_flow_errors.flow_id` is `ON DELETE SET NULL` by
    design (an error outlives its flow -- see `CeligoFlowError`). Left to the
    FK, a purged sandbox flow's errors would survive as orphans with
    `flow_id NULL`, counted nowhere and attributable to nothing (the
    independent-model review angle caught this; the Claude verifier had
    'refuted' it against a checkout without the purge). So the doomed flows'
    errors are deleted explicitly, first, inside the same transaction.

    `IS TRUE` / `is_(True)`, never truthiness: a NULL flag is production and
    must never be swept."""
    doomed_integrations = or_(
        CeligoIntegration.sandbox.is_(True),
        CeligoIntegration.celigo_id.in_(list(integration_celigo_ids)),
    )
    doomed_flow_ids = (
        select(CeligoFlow.id)
        .join(CeligoIntegration, CeligoIntegration.id == CeligoFlow.integration_id)
        .where(
            CeligoFlow.tenant_id == tenant_id,
            CeligoIntegration.tenant_id == tenant_id,
            CeligoIntegration.celigo_connection_id == connection_id,
            doomed_integrations,
        )
    )
    await db.execute(
        delete(CeligoFlowError).where(
            CeligoFlowError.tenant_id == tenant_id,
            CeligoFlowError.flow_id.in_(doomed_flow_ids),
        )
    )
    integrations_result = await db.execute(
        delete(CeligoIntegration).where(
            CeligoIntegration.tenant_id == tenant_id,
            CeligoIntegration.celigo_connection_id == connection_id,
            doomed_integrations,
        )
    )
    scripts_result = await db.execute(
        delete(CeligoScript).where(
            CeligoScript.tenant_id == tenant_id,
            CeligoScript.celigo_connection_id == connection_id,
            or_(CeligoScript.sandbox.is_(True), CeligoScript.celigo_id.in_(list(script_celigo_ids))),
        )
    )
    return (integrations_result.rowcount or 0), (scripts_result.rowcount or 0)


# ---------------------------------------------------------------------------
# celigo_flows
# ---------------------------------------------------------------------------


async def upsert_flow(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    integration_id: uuid.UUID,
    sanitized: dict,
) -> uuid.UUID:
    """Upsert one `celigo_flows` row from a SANITIZED flow payload (already
    through `sanitizer.sanitize("flow", raw)`)."""
    ai_description = sanitized.get("aiDescription") or {}
    values = dict(
        tenant_id=tenant_id,
        celigo_connection_id=connection_id,
        integration_id=integration_id,
        celigo_id=sanitized["_id"],
        name=sanitized.get("name", ""),
        disabled=sanitized.get("disabled"),
        schedule=sanitized.get("schedule"),
        timezone=sanitized.get("timezone"),
        last_executed_at=_parse_celigo_timestamp(sanitized.get("lastExecutedAt")),
        source_id=sanitized.get("_sourceId"),
        ai_description_summary=ai_description.get("summary"),
        ai_description_detailed=ai_description.get("detailed"),
        ai_description_generated_on=ai_description.get("generatedOn"),
        celigo_last_modified=_parse_celigo_timestamp(sanitized.get("lastModified")),
        raw_json=sanitized,
    )
    stmt = (
        insert(CeligoFlow)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_celigo_flows_identity",
            set_=_set_clause(values, exclude={"tenant_id", "celigo_connection_id", "celigo_id"}),
        )
        .returning(CeligoFlow.id)
    )
    return (await db.execute(stmt)).scalar_one()


# ---------------------------------------------------------------------------
# celigo_flow_steps -- extraction (pure) + upsert (DB)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlowStepInput:
    """One step extracted from a sanitized flow payload, ready to upsert."""

    celigo_id: str  # referenced _exportId / _importId
    role: str  # "generator" | "processor"
    router_id: str | None
    branch_id: str | None
    sequence: int
    filter_json: dict | None
    mapping_json: dict | None  # responseMapping
    proceed_on_failure: bool | None
    skip_retries: bool | None


def _generator_to_step(gen: dict, *, sequence: int) -> FlowStepInput | None:
    export_id = gen.get("_exportId")
    if not export_id:
        return None
    return FlowStepInput(
        celigo_id=export_id,
        role="generator",
        router_id=None,
        branch_id=None,
        sequence=sequence,
        filter_json=None,
        mapping_json=None,
        proceed_on_failure=None,
        skip_retries=gen.get("skipRetries"),
    )


def _processor_to_step(
    proc: dict, *, router_id: str | None, branch_id: str | None, sequence: int
) -> FlowStepInput | None:
    celigo_id = proc.get("_exportId") or proc.get("_importId")
    if not celigo_id:
        return None
    return FlowStepInput(
        celigo_id=celigo_id,
        role="processor",
        router_id=router_id,
        branch_id=branch_id,
        sequence=sequence,
        filter_json=proc.get("filter"),
        mapping_json=proc.get("responseMapping"),
        proceed_on_failure=proc.get("proceedOnFailure"),
        skip_retries=None,
    )


def extract_flow_steps(sanitized_flow: dict) -> list[FlowStepInput]:
    """Extract every page-generator/page-processor step from a SANITIZED flow
    payload (already through `sanitizer.sanitize("flow", raw)`), covering
    BOTH the top-level `pageGenerators`/`pageProcessors` arrays AND every
    `routers[].branches[].pageProcessors` array.

    THE TRAP: `routers[]` is a flat list -- every router that exists is
    already present in it. `nextRouterId` (observed-shapes.md) chains
    branches into a graph for CELIGO'S OWN UI/execution semantics ("this
    branch continues at router X"), but it is not needed to DISCOVER which
    routers exist; a flat iteration over `routers[]` already reaches every
    branch's `pageProcessors`. A reader that only looks at the top-level
    arrays silently misses most steps in exactly the multi-subsidiary
    sales-order flows the recon chain depends on (observed-shapes.md,
    live-probed across 60 real flows) -- this function is the fix.

    Steps with neither `_exportId` nor `_importId` are skipped (malformed --
    there is no id to key a `celigo_flow_steps` row on), never given a
    fabricated id.
    """
    steps: list[FlowStepInput] = []

    for i, gen in enumerate(sanitized_flow.get("pageGenerators") or []):
        step = _generator_to_step(gen, sequence=i)
        if step is not None:
            steps.append(step)

    for i, proc in enumerate(sanitized_flow.get("pageProcessors") or []):
        step = _processor_to_step(proc, router_id=None, branch_id=None, sequence=i)
        if step is not None:
            steps.append(step)

    for router in sanitized_flow.get("routers") or []:
        router_id = router.get("id")
        for branch in router.get("branches") or []:
            branch_id = branch.get("branchId")
            for i, proc in enumerate(branch.get("pageProcessors") or []):
                step = _processor_to_step(proc, router_id=router_id, branch_id=branch_id, sequence=i)
                if step is not None:
                    steps.append(step)

    return steps


async def upsert_flow_step(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    flow_id: uuid.UUID,
    step: FlowStepInput,
    adaptor_type: str | None = None,
    connection_celigo_id: str | None = None,
) -> uuid.UUID:
    """Upsert one `celigo_flow_steps` row. `adaptor_type`/`connection_celigo_id`
    live on the REFERENCED export/import object, not the step itself -- left
    NULL on a fresh row unless a caller has already fetched that object and
    can supply real values (never invented here).

    FIX ROUND 1 (Task 7, `sync_service.py`): these two columns are EXCLUDED
    from the ON CONFLICT DO UPDATE's SET clause whenever the caller passes
    `None` for them. Task 7's Phase B calls this function on every flow
    resync, BEFORE it has fetched the referenced export/import object --
    always with both params at their `None` default at that point in the
    pipeline. `sync_service.backfill_flow_step_reference_info` fills them in
    afterward, once per sync, from a LATER phase's export/import fetch. An
    unconditional overwrite here would silently wipe that backfill back to
    NULL on the very next flow resync -- confirmed by execution (a Task 7
    test failed exactly this way before this guard existed: a value
    backfilled in one sync run read back NULL after a second run's Phase B
    re-upserted the step with its default `None`). A caller that DOES have a
    real value still gets it written, on both the initial INSERT and a
    conflict -- this guard only protects against a caller's absence of
    information, never overrides one that has it.

    See module docstring point 2 for the role-collision guard."""
    values = dict(
        tenant_id=tenant_id,
        celigo_connection_id=connection_id,
        flow_id=flow_id,
        celigo_id=step.celigo_id,
        role=step.role,
        router_id=step.router_id,
        branch_id=step.branch_id,
        sequence=step.sequence,
        adaptor_type=adaptor_type,
        connection_celigo_id=connection_celigo_id,
        filter_json=step.filter_json,
        mapping_json=step.mapping_json,
        proceed_on_failure=step.proceed_on_failure,
        skip_retries=step.skip_retries,
    )
    # branch_key and dedup_key are STORED GENERATED -- deliberately never in
    # `values`. See module + model docstrings.
    conflict_exclude = {"tenant_id", "celigo_connection_id", "flow_id", "celigo_id", "role"}
    if adaptor_type is None:
        conflict_exclude.add("adaptor_type")
    if connection_celigo_id is None:
        conflict_exclude.add("connection_celigo_id")
    insert_stmt = insert(CeligoFlowStep).values(**values)
    stmt = insert_stmt.on_conflict_do_update(
        constraint="uq_celigo_flow_steps_identity",
        set_=_set_clause(values, exclude=conflict_exclude),
        # Guard: only apply the update if the existing row's role matches the
        # one being written. If it doesn't, this WHERE excludes the
        # conflicting row from the update entirely -- RETURNING yields zero
        # rows for it, which the code below treats as a role collision.
        where=(CeligoFlowStep.role == insert_stmt.excluded.role),
    ).returning(CeligoFlowStep.id)

    result = (await db.execute(stmt)).scalar_one_or_none()
    if result is None:
        raise FlowStepRoleCollisionError(
            flow_id=flow_id, celigo_id=step.celigo_id, branch_key_hint=step.branch_id, new_role=step.role
        )
    return result


async def sync_flow_steps(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    flow_id: uuid.UUID,
    steps: Iterable[FlowStepInput],
) -> list[uuid.UUID]:
    """Upsert every step in *steps* (typically `extract_flow_steps`' output)
    for one flow. Any `FlowStepRoleCollisionError` propagates immediately --
    never caught here, per module docstring point 2."""
    return [
        await upsert_flow_step(db, tenant_id=tenant_id, connection_id=connection_id, flow_id=flow_id, step=step)
        for step in steps
    ]


async def backfill_flow_step_reference_info(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    celigo_id: str,
    adaptor_type: str | None,
    connection_celigo_id: str | None,
    record_type: str | None = None,
    operation: str | None = None,
    search_id: str | None = None,
) -> int:
    """Task 7's export/import fetch phase: bulk-backfill `adaptor_type`/
    `connection_celigo_id`/`record_type`/`operation`/`search_id` onto EVERY
    `celigo_flow_steps` row that references `celigo_id` (the export/import
    object's own `_id`) -- filling in columns `upsert_flow_step`'s own
    docstring flags as "live on the REFERENCED export/import object...
    nullable here, filled in by whichever sync step fetches that object".
    The SAME export/import id can be referenced by more than one flow, or
    more than one branch within one flow (module docstring point 1) -- a
    plain `db.get()`-and-update would only touch the first row found; this
    statement updates ALL of them.

    `record_type`/`operation`/`search_id` are Task 11's provenance input
    (fix round 2, migration 096) -- `record_type` is shared across import
    and export callers (same semantic field, different parent key on the
    wire: `netsuite_da.recordType` vs `netsuite.restlet.recordType`);
    `operation` is import-only and `search_id` is export-only, so a caller
    backfilling from the "wrong" kind simply never passes the other one,
    same as `adaptor_type`/`connection_celigo_id` already worked.

    Only fields that are non-`None` are included in the `SET` clause: an
    export/import fetch that happens to omit one of these on a resync (a
    projection quirk, or genuinely the wrong kind for that field) must never
    blank out a previously-known value -- an unconditional `UPDATE ... SET
    x = NULL` would silently regress a prior successful backfill (confirmed
    by execution once already, for `adaptor_type`/`connection_celigo_id` --
    see `upsert_flow_step`'s own FIX ROUND 1 docstring for the sibling bug
    this same discipline prevents). Returns the number of rows updated (`0`
    is a legitimate result: no currently-synced step references this
    `celigo_id` yet, or none of the fields had a real value to write)."""
    values: dict = {}
    if adaptor_type is not None:
        values["adaptor_type"] = adaptor_type
    if connection_celigo_id is not None:
        values["connection_celigo_id"] = connection_celigo_id
    if record_type is not None:
        values["record_type"] = record_type
    if operation is not None:
        values["operation"] = operation
    if search_id is not None:
        values["search_id"] = search_id
    if not values:
        return 0
    values["updated_at"] = func.now()
    stmt = (
        update(CeligoFlowStep)
        .where(
            CeligoFlowStep.tenant_id == tenant_id,
            CeligoFlowStep.celigo_connection_id == connection_id,
            CeligoFlowStep.celigo_id == celigo_id,
        )
        .values(**values)
    )
    result = await db.execute(stmt)
    return result.rowcount


# ---------------------------------------------------------------------------
# celigo_scripts + dedup
# ---------------------------------------------------------------------------


async def upsert_script(
    db: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, sanitized: dict
) -> uuid.UUID:
    """Upsert one `celigo_scripts` row from a SANITIZED script payload
    (already through `sanitizer.sanitize("script", raw)`). `content_hash` is
    computed here, never trusted from a caller (see `_content_hash`)."""
    content = sanitized.get("content")
    values = dict(
        tenant_id=tenant_id,
        celigo_connection_id=connection_id,
        celigo_id=sanitized["_id"],
        name=sanitized.get("name", ""),
        content=content,
        content_hash=_content_hash(content),
        source_id=sanitized.get("_sourceId"),
        sandbox=sanitized.get("sandbox"),
        celigo_last_modified=_parse_celigo_timestamp(sanitized.get("lastModified")),
    )
    # dedup_key is STORED GENERATED -- deliberately never in `values`.
    stmt = (
        insert(CeligoScript)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_celigo_scripts_identity",
            set_=_set_clause(values, exclude={"tenant_id", "celigo_connection_id", "celigo_id"}),
        )
        .returning(CeligoScript.id)
    )
    return (await db.execute(stmt)).scalar_one()


@dataclass(frozen=True)
class LogicalScript:
    """One dedup group of `celigo_scripts` rows sharing a `dedup_key` -- the
    clone original plus every clone that points at it (observed-shapes.md,
    live-confirmed: a clone carries `_sourceId` pointing at the original; the
    original itself has no `_sourceId`, so `dedup_key = COALESCE(source_id,
    celigo_id)` puts both under the original's own `celigo_id`)."""

    dedup_key: str
    name: str
    content_hash: str | None
    script_ids: tuple[uuid.UUID, ...]
    celigo_ids: tuple[str, ...]
    attachment_count: int
    # Best-effort flag: True if the group's members don't all share the same
    # content_hash (a clone that has since diverged from its original).
    # Never drops a member from the group over this -- it only annotates.
    content_diverged: bool


async def list_logical_scripts(
    db: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> list[LogicalScript]:
    """Group every synced `celigo_scripts` row for this connection by
    `dedup_key`, collapsing a script's clone family into one logical script
    with attachment counts summed across every member's `script_celigo_id`
    (attachments are keyed to the RAW Celigo id of whichever clone was
    actually attached, not to the logical group)."""
    scripts = (
        (
            await db.execute(
                select(CeligoScript).where(
                    CeligoScript.tenant_id == tenant_id,
                    CeligoScript.celigo_connection_id == connection_id,
                    # Production only -- a clone family must not count its
                    # sandbox copies (132 of 259 scripts on the live account).
                    celigo_script_is_production(),
                )
            )
        )
        .scalars()
        .all()
    )
    if not scripts:
        return []

    groups: dict[str, list[CeligoScript]] = defaultdict(list)
    for script in scripts:
        groups[script.dedup_key].append(script)

    all_celigo_ids = [s.celigo_id for s in scripts]
    counts_result = await db.execute(
        select(CeligoScriptAttachment.script_celigo_id, func.count())
        .where(
            CeligoScriptAttachment.tenant_id == tenant_id,
            CeligoScriptAttachment.celigo_connection_id == connection_id,
            CeligoScriptAttachment.script_celigo_id.in_(all_celigo_ids),
        )
        .group_by(CeligoScriptAttachment.script_celigo_id)
    )
    attachment_counts_by_celigo_id: dict[str, int] = dict(counts_result.all())

    out: list[LogicalScript] = []
    for dedup_key, members in groups.items():
        # The ORIGINAL (no source_id -> celigo_id == dedup_key) is the
        # representative when it's been synced; otherwise fall back to the
        # earliest-created member -- deterministic either way.
        representative = next((m for m in members if m.celigo_id == dedup_key), None)
        if representative is None:
            representative = min(members, key=lambda m: (m.created_at, m.celigo_id))

        celigo_ids = tuple(m.celigo_id for m in members)
        attachment_count = sum(attachment_counts_by_celigo_id.get(cid, 0) for cid in celigo_ids)
        hashes = {m.content_hash for m in members if m.content_hash is not None}

        out.append(
            LogicalScript(
                dedup_key=dedup_key,
                name=representative.name,
                content_hash=representative.content_hash,
                script_ids=tuple(m.id for m in members),
                celigo_ids=celigo_ids,
                attachment_count=attachment_count,
                content_diverged=len(hashes) > 1,
            )
        )
    return out


# ---------------------------------------------------------------------------
# celigo_script_attachments
# ---------------------------------------------------------------------------


def qualify_json_path(json_path: str, *, reference_object_celigo_id: str | None) -> str:
    """Make a `ScriptRef.json_path` unique within its flow.

    A ref walked off the flow object is already flow-relative and is returned
    unchanged. A ref walked off an export/import is relative to THAT object
    (`transform.script`), which two different objects in the same flow can
    produce identically -- so it is prefixed with the owning object's celigo
    id (`imp_1.transform.script`). Celigo ids never collide with a
    flow-relative path's first segment (`pageGenerators`/`pageProcessors`/
    `routers`/`hooks`/`transform`/`filter`/`script`/`$`), so the two forms
    stay distinguishable.

    STABILITY -- TRUE FOR ONE HALF, NOT THE OTHER (corrected, scoped
    re-review R5, 2026-08-27; the previous wording claimed both halves were
    "stable across syncs", and the second half was proven false by
    execution):

      * QUALIFIED (`imp_1.transform.script`): stable. The prefix is Celigo's
        own id and the suffix is a path INSIDE that object, so neither moves
        when the flow around it changes. Two full syncs of an unchanged flow
        stay at the same rows -- that is what keeps `ON CONFLICT DO UPDATE`
        idempotent instead of inserting a fresh row every night.
      * UNQUALIFIED (`routers[0].script`): NOT stable. A flow-relative path
        is INDEX-BEARING, so removing the first router shifts the second one
        from `routers[1]` to `routers[0]`.

    PRE-EXISTING GAP, NAMED SO IT IS FINDABLE (not introduced here, and
    deliberately not fixed here): index-bearing paths compose with the
    absence of any prune -- nothing in this package ever DELETEs a
    `celigo_script_attachments` row -- so a SHRINKING flow leaves the vacated
    path behind while the surviving script is re-written at the index it
    shifted into. The result is a phantom: one script shown as attached at
    two router sites, one of which no longer exists. Pinned by
    `tests/test_celigo_sync.py::TestAttachmentPathsAreIndexBasedNotStable`,
    which executes the two syncs. Closing it takes a prune step keyed on what
    the current sync actually observed (or attachment identity that does not
    embed an array index) -- both bigger than a docstring, hence the test.

    Pure and separately callable so a reader can see the whole
    identity rule in one place; `upsert_script_attachment` applies it, no
    caller should.
    """
    if reference_object_celigo_id is None:
        return json_path
    return f"{reference_object_celigo_id}.{json_path}"


async def upsert_script_attachment(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    flow_id: uuid.UUID,
    flow_step_id: uuid.UUID | None,
    script_id: uuid.UUID | None,
    script_celigo_id: str,
    function_name: str | None,
    json_path: str,
    reference_object_celigo_id: str | None,
    site_type: str | None,
) -> uuid.UUID:
    """Upsert one `celigo_script_attachments` row.

    `json_path` is IDENTITY, not decoration -- the unique key is `(tenant_id,
    flow_id, json_path)`. Pass it EXACTLY as `graph.walk_script_refs` emitted
    it (`ScriptRef.json_path`); never normalise, trim or reformat it. The one
    transformation allowed is the QUALIFICATION below, and it happens here so
    that no caller can do it differently or forget it.

    `reference_object_celigo_id` says which object the ref was walked from,
    and is REQUIRED -- there is no default, so a caller has to answer the
    question rather than inherit a wrong guess (see finding 1 below):

      * `None` -- the ref came from the FLOW object itself, so its path is
        already flow-relative and is stored unchanged.
      * a celigo id -- the ref came from an export/import that the flow only
        REFERENCES by id, so its path is relative to that object, not to the
        flow, and is qualified with the id (`qualify_json_path`).

    WHY (whole-branch review finding 1): Phase D walks each export/import
    separately, so two imports in one flow that each carry a script at
    `transform.script` produced the SAME `(flow_id, json_path)`. The second
    `ON CONFLICT DO UPDATE` overwrote the first -- including
    `script_celigo_id` -- silently, with the sync reporting success. Multi-
    step NetSuite flows are the ordinary case and `transform.script` is the
    most-used attachment site live, so this was the core deliverable losing
    data. Qualifying makes the path unique within the flow BY CONSTRUCTION,
    which is why the constraint needed no migration.

    Adding `flow_step_id` to the unique key instead would have been the wrong
    fix twice over: that column is NULLABLE (a router-level ref belongs to no
    step), so NULL-is-distinct would let two rows with the same path both
    insert and break idempotency -- the exact trap `celigo_flow_steps` avoids
    with its STORED GENERATED `branch_key`.
    """
    json_path = qualify_json_path(json_path, reference_object_celigo_id=reference_object_celigo_id)
    values = dict(
        tenant_id=tenant_id,
        celigo_connection_id=connection_id,
        flow_id=flow_id,
        flow_step_id=flow_step_id,
        script_id=script_id,
        script_celigo_id=script_celigo_id,
        function_name=function_name,
        json_path=json_path,
        site_type=site_type,
    )
    stmt = (
        insert(CeligoScriptAttachment)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_celigo_script_attachments_identity",
            set_=_set_clause(values, exclude={"tenant_id", "flow_id", "json_path"}),
        )
        .returning(CeligoScriptAttachment.id)
    )
    return (await db.execute(stmt)).scalar_one()


async def upsert_script_attachment_from_ref(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    flow_id: uuid.UUID,
    flow_step_id: uuid.UUID | None,
    ref: ScriptRef,
    reference_object_celigo_id: str | None,
    script_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Convenience wrapper taking `graph.walk_script_refs`' own output type
    directly -- the natural way Task 2's walker and this repository compose.

    `reference_object_celigo_id` is required here for the same reason it is
    required on `upsert_script_attachment`: a `ScriptRef` does not record
    which object it was walked from, and guessing wrong silently overwrites
    another step's attachment. A caller that does not pass it gets a
    TypeError, not a collision."""
    return await upsert_script_attachment(
        db,
        tenant_id=tenant_id,
        connection_id=connection_id,
        flow_id=flow_id,
        flow_step_id=flow_step_id,
        script_id=script_id,
        script_celigo_id=ref.script_id,
        function_name=ref.function_name,
        json_path=ref.json_path,
        reference_object_celigo_id=reference_object_celigo_id,
        site_type=ref.site_type,
    )


async def backfill_attachment_script_ids(
    db: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, script_ids: dict[str, uuid.UUID]
) -> int:
    """Bulk-fill `script_id` on any `celigo_script_attachments` row still
    NULL, now that *script_ids* (celigo_id -> local id) has entries for it.

    WHOLE-BRANCH REVIEW FINDING 6 (2026-08-27, PROVEN across two full syncs
    -- not a first-run artifact): `sync_service.py`'s Phase B records
    flow/router-level attachments (the flow object's own script refs, e.g. a
    `routers[].script`) via `_record_attachments`, which resolves `script_id`
    from a `script_ids` map that Phase C -- run LATER in the same pass -- is
    what actually populates. Phase B's `script_ids` argument is therefore
    always `{}` at that call site, every single run, so `script_id` stayed
    NULL forever, exactly the same "column that can never be non-NULL" shape
    as the already-fixed permanently-NULL `celigo_last_modified` bug. Phase
    D's export/import-level attachments never had this problem because Phase
    D runs AFTER Phase C, so its own `_record_attachments` call already sees
    a populated map.

    This function is `sync_flow_map_for_connection`'s fix: called once,
    right after Phase C finishes syncing every script, it goes back and
    fills in `script_id` for whatever Phase B already wrote with it NULL --
    the same "backfill once the dependency exists" shape
    `backfill_flow_step_reference_info` already uses for
    `adaptor_type`/`connection_celigo_id`.

    Scoped with `script_id IS NULL` so it only ever touches rows nothing has
    resolved yet -- never overwrites a real value with a different one, and
    a script that genuinely never syncs (deleted, or lagging past this run)
    correctly leaves the attachment's `script_id` NULL rather than guessing.
    One UPDATE per distinct `script_celigo_id` in *script_ids* -- Phase C's
    script count is small (this codebase's own account has under a few
    hundred), so a per-script round trip costs nothing meaningful; a single
    `VALUES`-joined UPDATE would read no differently to a caller and was not
    worth the extra complexity here. Returns the number of rows updated
    across every script (`0` is legitimate: nothing was waiting on any of
    these scripts)."""
    if not script_ids:
        return 0
    total = 0
    for script_celigo_id, script_local_id in script_ids.items():
        stmt = (
            update(CeligoScriptAttachment)
            .where(
                CeligoScriptAttachment.tenant_id == tenant_id,
                CeligoScriptAttachment.celigo_connection_id == connection_id,
                CeligoScriptAttachment.script_celigo_id == script_celigo_id,
                CeligoScriptAttachment.script_id.is_(None),
            )
            .values(script_id=script_local_id, updated_at=func.now())
        )
        result = await db.execute(stmt)
        total += result.rowcount
    return total


# ---------------------------------------------------------------------------
# celigo_error_signatures
# ---------------------------------------------------------------------------


async def upsert_error_signature(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    fingerprint: str,
    source: str | None = None,
    code: str | None = None,
    sample_message: str | None = None,
    occurrence_count: int | None = None,
    first_seen: datetime | None = None,
    last_seen: datetime | None = None,
) -> uuid.UUID:
    """Upsert one `celigo_error_signatures` row. Aggregation semantics
    (incrementing `occurrence_count`, advancing `first_seen`/`last_seen`
    across repeated sightings of the same fingerprint) are the caller's job
    (e.g. an error-normalizer) -- this function stores whatever values it's
    given, the same as every other upsert in this module; it does not invent
    increment-on-conflict SQL. `sample_message` is PII-bearing (never logged
    by this function or any caller).

    `connection_id` is REQUIRED, not Optional, even though the underlying
    column is nullable -- see module docstring point 4 (NULL-is-DISTINCT
    trap: an upsert with `connection_id=None` would never conflict, so it
    would silently insert a new row on every call instead of updating)."""
    if connection_id is None:
        raise ValueError(
            "upsert_error_signature requires a real connection_id -- see module docstring point 4 "
            "(NULL-is-DISTINCT would make ON CONFLICT never fire, silently duplicating rows)"
        )
    values: dict = dict(
        tenant_id=tenant_id,
        celigo_connection_id=connection_id,
        fingerprint=fingerprint,
        source=source,
        code=code,
        sample_message=sample_message,
    )
    if occurrence_count is not None:
        values["occurrence_count"] = occurrence_count
    if first_seen is not None:
        values["first_seen"] = first_seen
    if last_seen is not None:
        values["last_seen"] = last_seen

    stmt = (
        insert(CeligoErrorSignature)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_celigo_error_signatures_identity",
            set_=_set_clause(values, exclude={"tenant_id", "celigo_connection_id", "fingerprint"}),
        )
        .returning(CeligoErrorSignature.id)
    )
    return (await db.execute(stmt)).scalar_one()


# ---------------------------------------------------------------------------
# celigo_flow_errors -- NEVER DELETED. resolved_at/purged_at only.
# ---------------------------------------------------------------------------


async def upsert_flow_error(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    celigo_id: str,
    flow_id: uuid.UUID | None = None,
    flow_step_id: uuid.UUID | None = None,
    signature_id: uuid.UUID | None = None,
    trace_key: str | None = None,
    retry_data_key: str | None = None,
    source: str | None = None,
    code: str | None = None,
    message: str | None = None,
    occurred_at: datetime | None = None,
    purge_at: datetime | None = None,
    flow_job_id: str | None = None,
    retriable: bool | None = None,
) -> uuid.UUID:
    """Upsert one `celigo_flow_errors` row. `message` is PII-bearing
    (sanitizer.py's own docstring: kept verbatim by design, "the message IS
    the diagnosis") -- this function never logs it, and no caller should
    either. See module docstring point 3: there is no delete function for
    this table anywhere in this module.

    `connection_id` is REQUIRED, not Optional, even though the underlying
    column is nullable -- see module docstring point 4 (same NULL-is-DISTINCT
    trap as `upsert_error_signature`)."""
    if connection_id is None:
        raise ValueError(
            "upsert_flow_error requires a real connection_id -- see module docstring point 4 "
            "(NULL-is-DISTINCT would make ON CONFLICT never fire, silently duplicating rows)"
        )
    values = dict(
        tenant_id=tenant_id,
        celigo_connection_id=connection_id,
        flow_id=flow_id,
        flow_step_id=flow_step_id,
        signature_id=signature_id,
        celigo_id=celigo_id,
        trace_key=trace_key,
        retry_data_key=retry_data_key,
        source=source,
        code=code,
        message=message,
        occurred_at=occurred_at,
        purge_at=purge_at,
        flow_job_id=flow_job_id,
        retriable=retriable,
    )
    stmt = (
        insert(CeligoFlowError)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_celigo_flow_errors_identity",
            set_=_set_clause(values, exclude={"tenant_id", "celigo_connection_id", "celigo_id"}),
        )
        .returning(CeligoFlowError.id)
    )
    return (await db.execute(stmt)).scalar_one()


async def mark_flow_errors_resolved(
    db: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, celigo_ids: Iterable[str]
) -> int:
    """Mark the given, currently-unresolved errors resolved
    (`resolved_at = now()`). NEVER deletes a row -- the caller decides which
    celigo_ids count as "resolved" (e.g. no longer listed by Celigo and
    before their `purge_at`); this function only applies the transition.
    Idempotent: re-running with the same ids only touches rows not already
    resolved. Returns the number of rows updated."""
    celigo_ids = list(celigo_ids)
    if not celigo_ids:
        return 0
    stmt = (
        update(CeligoFlowError)
        .where(
            CeligoFlowError.tenant_id == tenant_id,
            CeligoFlowError.celigo_connection_id == connection_id,
            CeligoFlowError.celigo_id.in_(celigo_ids),
            CeligoFlowError.resolved_at.is_(None),
        )
        .values(resolved_at=func.now())
    )
    result = await db.execute(stmt)
    return result.rowcount


# ---------------------------------------------------------------------------
# celigo_config_changes -- Task 7's drift log (migration 095). Extends this
# module's scope from the seven migration-094 tables to an eighth: still the
# storage layer, still trusting an already-computed diff from its caller
# (app.services.celigo.sync_service owns deciding WHAT changed; this function
# only records that it did).
# ---------------------------------------------------------------------------


async def insert_config_change(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    object_kind: str,
    object_id: uuid.UUID | None,
    celigo_id: str,
    flow_id: uuid.UUID | None,
    field: str,
    old_value: object,
    new_value: object,
) -> uuid.UUID:
    """Append one drift-detection row. A plain INSERT, not an upsert --
    unlike every other function in this module, each call is a genuinely NEW
    historical fact ("field X changed from A to B, observed now"), not a
    current-state mirror converging onto one row per identity. See
    `app/models/celigo.py`'s `CeligoConfigChange` docstring for the
    polymorphic `object_kind`/`object_id` design and why `old_value`/
    `new_value` are untyped JSONB rather than per-field columns."""
    change = CeligoConfigChange(
        tenant_id=tenant_id,
        celigo_connection_id=connection_id,
        flow_id=flow_id,
        object_kind=object_kind,
        object_id=object_id,
        celigo_id=celigo_id,
        field=field,
        old_value=old_value,
        new_value=new_value,
    )
    db.add(change)
    await db.flush()
    return change.id


async def mark_flow_errors_purged(
    db: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, celigo_ids: Iterable[str]
) -> int:
    """Mark the given errors purged (`purged_at = now()`) -- Celigo's own
    ~30-day deletion caught up with them (the caller decides this, typically
    by comparing against each error's own `purge_at`). NEVER deletes the
    local row -- surviving Celigo's purge is this feature's entire reason to
    exist. Idempotent, same shape as `mark_flow_errors_resolved`. Returns the
    number of rows updated."""
    celigo_ids = list(celigo_ids)
    if not celigo_ids:
        return 0
    stmt = (
        update(CeligoFlowError)
        .where(
            CeligoFlowError.tenant_id == tenant_id,
            CeligoFlowError.celigo_connection_id == connection_id,
            CeligoFlowError.celigo_id.in_(celigo_ids),
            CeligoFlowError.purged_at.is_(None),
        )
        .values(purged_at=func.now())
    )
    result = await db.execute(stmt)
    return result.rowcount
