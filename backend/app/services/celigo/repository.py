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

from sqlalchemy import func, select, update
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
    NULL unless a caller has already fetched that object and can supply real
    values (never invented here). See module docstring point 2 for the
    role-collision guard."""
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
    insert_stmt = insert(CeligoFlowStep).values(**values)
    stmt = insert_stmt.on_conflict_do_update(
        constraint="uq_celigo_flow_steps_identity",
        set_=_set_clause(values, exclude={"tenant_id", "celigo_connection_id", "flow_id", "celigo_id", "role"}),
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
    site_type: str | None,
) -> uuid.UUID:
    """Upsert one `celigo_script_attachments` row. `json_path` is IDENTITY,
    not decoration (migration's unique key is `(tenant_id, flow_id,
    json_path)`) -- pass it through EXACTLY as `graph.walk_script_refs`
    emitted it (`ScriptRef.json_path`); never normalise, trim, or reformat it
    here or in any caller."""
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
    script_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Convenience wrapper taking `graph.walk_script_refs`' own output type
    directly -- the natural way Task 2's walker and this repository compose."""
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
        site_type=ref.site_type,
    )


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
