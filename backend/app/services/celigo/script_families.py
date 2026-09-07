"""Task 1 -- clone-family facts for the account-wide Celigo Scripts view.

Spec: `docs/superpowers/specs/2026-09-06-celigo-scripts-view-design.md` §2.
A NEW module, separate from `read_queries.py` (which the AST guard keeps
content-free -- `test_celigo_read_queries_parity.py::TestNoScriptContentSelected`).

Guarded against ever reaching a chat/MCP surface by TWO layers
(`test_celigo_read_queries_parity.py::TestScriptFamiliesNeverImportedByChatSurfaces`):
three narrow direct-import AST scans (fast, exact for `read_queries.py`, the
celigo flow-map MCP tool, and every file under `services/chat/`), plus
`test_chat_and_mcp_surfaces_cannot_import_script_families_at_runtime`, which
enforces the invariant with Python's REAL import machinery instead of a
hand-rolled transitive AST walker -- an earlier version of that walker had
holes (`import a.b.c` never visited `a/__init__.py` or `a/b/__init__.py`, so
a leak hidden in a parent package's `__init__` escaped it; a bare `import
app` crashed it outright). The runtime test installs a `sys.meta_path`
finder that raises for `fullname == "app.services.celigo.script_families"`,
then actually imports `read_queries.py` and every module under
`app/services/chat/` and `app/mcp/` (discovered via `pkgutil.walk_packages`)
-- if any import chain, however indirect, ever reaches this module, Python's
own import system raises, no exceptions swallowed. A `TYPE_CHECKING`-guarded
import gets no special case: it never executes at runtime, so the runtime
guard correctly never sees it -- that absence IS the right semantics, not a
gap. This module IS where script content is allowed to reach the API (the
detail's members), because it serves a human-only surface, never a chat
tool.

Reuses `celigo_script_is_production()` (production-only, same rule
`repository.list_logical_scripts` uses) and the family grouping key
(`CeligoScript.dedup_key`, itself the DB-computed
`COALESCE(source_id, celigo_id)` -- see `app/models/celigo.py`). Version
letters are computed by `topology.assign_version_letters`, extracted so this
module and `topology.script_family_facts` agree on ONE ordering rule
(earliest `celigo_last_modified` per distinct content hash, ties broken by
the hash itself) rather than two copies that could drift; `topology`'s own
single-copy-family special case is NOT part of the shared helper -- see that
function's docstring for why the Scripts view needs a version even for a
one-copy family (a family with content always has at least one version card)
while the flow map's inline chip does not.

Bounded query count (no N+1), per function:
  * `get_script_family`: (1) a LIGHT scan of every production script for the
    connection, `.content` never selected (`load_only`) -- needed for BOTH
    grouping into families to find the target's own members, and every OTHER
    family's name, so `other_families_with_name` is computed from names
    alone and never needs a second full-content pass; (2) a FULL fetch
    (content included), scoped to only the target family's own `celigo_id`s;
    (3) one join across attachments/flows/integrations/steps, scoped to the
    target family's own `celigo_id`s; (4) one open-error aggregate grouped by
    `flow_step_id`, scoped to the step ids the join actually returned.
  * `list_script_families`: (1) the same light, content-free scan as above,
    scoped to EVERY celigo_id in the connection (the list endpoint never
    loads `.content` for anyone); (2) one join across attachments/flows/
    integrations/steps, scoped to every celigo_id in the connection; (3) one
    open-error aggregate grouped by `flow_step_id`; plus (4) one
    `cursor_states` read for `synced_at` and (5) one flow-count read for
    `totals.flows_total` (a production flow with zero script attachments
    never appears in the join above, so it needs its own count to be counted
    at all). A script's `size_bytes` is computed IN SQL (`octet_length`) in
    the SAME query as the light scan, never a separate query and never by
    touching `.content` in Python.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.models.celigo import (
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
from app.services.celigo.topology import assign_version_letters

_SYNC_OBJECT_TYPE = "celigo_flow_map"  # matches read_queries.sync_status's own CursorState.object_type


# ---------------------------------------------------------------------------
# Dataclasses (frozen) -- field-for-field, spec §2.2.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScriptFamilySite:
    attachment_id: uuid.UUID
    script_id: uuid.UUID | None
    script_celigo_id: str
    version_letter: str | None
    integration_id: uuid.UUID | None
    integration_name: str | None
    flow_id: uuid.UUID
    flow_name: str
    flow_disabled: bool | None
    flow_step_id: uuid.UUID | None
    step_reference_name: str | None
    step_role: str | None
    step_adaptor_type: str | None
    step_record_type: str | None
    step_operation: str | None
    json_path: str
    function_name: str | None
    site_type: str
    open_error_count: int | None
    errors_checked_at: datetime | None


@dataclass(frozen=True)
class ScriptFamilyMember:
    script_id: uuid.UUID
    celigo_id: str
    name: str
    is_original: bool
    version_letter: str | None
    content_hash: str | None
    size_bytes: int | None
    celigo_last_modified: datetime | None
    sites_count: int
    flows_count: int
    content: str | None


@dataclass(frozen=True)
class ScriptFamilyVersion:
    letter: str
    content_hash: str
    copies_count: int
    sites_count: int
    first_seen: datetime | None
    size_bytes: int | None
    holds_original: bool


@dataclass(frozen=True)
class ScriptFamilySummary:
    dedup_key: str
    name: str
    kind: str
    function_name: str | None
    copies_count: int
    versions_count: int
    content_diverged: bool
    original_present: bool
    sites_count: int
    flows_count: int
    integrations_count: int
    integration_ids: list[uuid.UUID]
    flow_names: list[str]
    sites_with_open_errors: int
    sites_unchecked: int
    first_modified: datetime | None
    last_modified: datetime | None
    max_size_bytes: int | None
    other_families_with_name: int


@dataclass(frozen=True)
class ScriptFamilyTotals:
    scripts: int
    families: int
    attached_families: int
    unattached_families: int
    diverged_families: int
    sites: int
    flows_with_sites: int
    flows_total: int
    integrations_with_sites: int
    sites_with_open_errors: int


@dataclass(frozen=True)
class ScriptFamiliesList:
    totals: ScriptFamilyTotals
    families: list[ScriptFamilySummary]
    synced_at: datetime | None


@dataclass(frozen=True)
class ScriptFamilyDetail:
    summary: ScriptFamilySummary
    members: list[ScriptFamilyMember]
    versions: list[ScriptFamilyVersion]
    sites: list[ScriptFamilySite]


# ---------------------------------------------------------------------------
# Internal: one joined attachment/flow/integration/step row.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SiteRow:
    attachment: CeligoScriptAttachment
    flow_name: str
    flow_disabled: bool | None
    integration_id: uuid.UUID
    integration_name: str
    errors_checked_at: datetime | None
    step_role: str | None
    step_adaptor_type: str | None
    step_record_type: str | None
    step_operation: str | None
    step_reference_name: str | None


def _earliest_member(members: list[CeligoScript]) -> CeligoScript:
    """Oldest by `celigo_last_modified` (None sorts last), ties by `celigo_id`
    -- same idiom as `topology.script_family_facts`'s own member ordering."""
    return min(members, key=lambda s: (s.celigo_last_modified is None, s.celigo_last_modified, s.celigo_id))


def _is_original(script: CeligoScript) -> bool:
    """True if *script* is the clone family's original, never a copy --
    `dedup_key = COALESCE(source_id, celigo_id)` (a DB-generated column, see
    `app/models/celigo.py`) means the original is the one row whose OWN
    `celigo_id` equals the family's `dedup_key`; every clone's `dedup_key`
    instead points back at the original's `celigo_id`. The ONE place this
    predicate is written -- every other spot in this module calls it."""
    return script.celigo_id == script.dedup_key


def _family_name(members: list[CeligoScript]) -> str:
    """The original's name if it is present in production, else the
    earliest-modified member's (spec §1 item 4 / §2.2 name rule)."""
    original = next((m for m in members if _is_original(m)), None)
    if original is not None:
        return original.name
    return _earliest_member(members).name


def _pick_mode(values: list[str]) -> str | None:
    """Most common value; ties broken alphabetically. `None` when *values*
    (already filtered of empties by the caller) is empty."""
    values = [v for v in values if v]
    if not values:
        return None
    counts = Counter(values)
    top = max(counts.values())
    return sorted(v for v, c in counts.items() if c == top)[0]


_DECLARED_KINDS = frozenset({"hook", "transform", "filter", "router"})


def _kind_from_site_types(site_types: set[str]) -> str:
    """`kind` is a closed enum (spec §2.2): hook | transform | filter |
    router | mixed | unattached. `site_types` here is the raw
    `attachment.site_type` values (already `"unknown"`-filled for a NULL
    column by the caller) -- `graph.py::_classify_site_type` documents
    `"unknown"` as a real, live-observed value for any `_scriptId` found
    outside a recognized path segment, so it is NOT one of the four
    declared per-site kinds and must never leak through as-is. A family
    whose sites are entirely (or partly) `"unknown"` reads as `mixed`: its
    sites don't agree on one declared kind, which is exactly what `mixed`
    already means for a family that spans several declared kinds."""
    if not site_types:
        return "unattached"
    if len(site_types) == 1:
        only = next(iter(site_types))
        return only if only in _DECLARED_KINDS else "mixed"
    return "mixed"


def _group_scripts(scripts: list[CeligoScript]) -> dict[str, list[CeligoScript]]:
    groups: dict[str, list[CeligoScript]] = defaultdict(list)
    for s in scripts:
        groups[s.dedup_key].append(s)
    return groups


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


async def _fetch_production_scripts(
    db: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, load_content: bool
) -> list[tuple[CeligoScript, int | None]]:
    """Every production script under *connection_id*, paired with its
    content's byte length computed IN SQL (`octet_length`, never Python's
    `len(content.encode("utf-8"))`) so a caller always has `size_bytes`
    without ever touching `.content`.

    `load_content=False` (`list_script_families`'s own path, and
    `get_script_family`'s account-wide grouping/name scan) applies
    `load_only` over every OTHER column this module needs -- `.content` is
    left deferred/unloaded on the returned rows, so nothing downstream may
    read it without triggering its own extra per-row SELECT (the N+1 this
    guards against). `load_content=True` loads the row in full; only
    `get_script_family` uses it, and only via `_fetch_scripts_by_celigo_id`
    below, scoped to the one family being requested -- this function itself
    is never called with `load_content=True` account-wide.
    """
    size_expr = func.octet_length(CeligoScript.content).label("size_bytes")
    stmt = select(CeligoScript, size_expr).where(
        CeligoScript.tenant_id == tenant_id,
        CeligoScript.celigo_connection_id == connection_id,
        celigo_script_is_production(),
    )
    if not load_content:
        stmt = stmt.options(
            load_only(
                CeligoScript.id,
                CeligoScript.celigo_id,
                CeligoScript.name,
                CeligoScript.dedup_key,
                CeligoScript.content_hash,
                CeligoScript.celigo_last_modified,
                CeligoScript.sandbox,
            )
        )
    rows = (await db.execute(stmt)).all()
    return [(script, size_bytes) for script, size_bytes in rows]


async def _fetch_scripts_by_celigo_id(
    db: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, celigo_ids: list[str]
) -> list[tuple[CeligoScript, int | None]]:
    """Full `CeligoScript` rows (content included), paired with `size_bytes`
    the same way `_fetch_production_scripts` does, for exactly *celigo_ids*
    -- `get_script_family`'s OWN family, never account-wide. Still
    production-only as defense in depth, even though *celigo_ids* is always
    already derived from a production-only scan by the caller."""
    if not celigo_ids:
        return []
    size_expr = func.octet_length(CeligoScript.content).label("size_bytes")
    stmt = select(CeligoScript, size_expr).where(
        CeligoScript.tenant_id == tenant_id,
        CeligoScript.celigo_connection_id == connection_id,
        CeligoScript.celigo_id.in_(celigo_ids),
        celigo_script_is_production(),
    )
    rows = (await db.execute(stmt)).all()
    return [(script, size_bytes) for script, size_bytes in rows]


async def _fetch_sites(
    db: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, celigo_ids: list[str]
) -> list[_SiteRow]:
    """One join across attachments/flows/(production)integrations/steps,
    scoped to *celigo_ids*. Production only: a site under a sandbox
    integration is not a site (`celigo_integration_is_production()` on the
    integration join's ON clause, not a trailing WHERE, so the OUTER step
    join below can't have its NULL rows dropped by a WHERE-clause
    predicate)."""
    if not celigo_ids:
        return []
    stmt = (
        select(
            CeligoScriptAttachment,
            CeligoFlow.name,
            CeligoFlow.disabled,
            CeligoFlow.integration_id,
            CeligoFlow.errors_checked_at,
            CeligoIntegration.name,
            CeligoFlowStep.role,
            CeligoFlowStep.adaptor_type,
            CeligoFlowStep.record_type,
            CeligoFlowStep.operation,
            CeligoFlowStep.reference_name,
        )
        .join(
            CeligoFlow,
            and_(CeligoFlow.id == CeligoScriptAttachment.flow_id, CeligoFlow.tenant_id == tenant_id),
        )
        .join(
            CeligoIntegration,
            and_(
                CeligoIntegration.id == CeligoFlow.integration_id,
                CeligoIntegration.tenant_id == tenant_id,
                celigo_integration_is_production(),
            ),
        )
        .outerjoin(
            CeligoFlowStep,
            and_(
                CeligoFlowStep.id == CeligoScriptAttachment.flow_step_id,
                CeligoFlowStep.tenant_id == tenant_id,
            ),
        )
        .where(
            CeligoScriptAttachment.tenant_id == tenant_id,
            CeligoScriptAttachment.celigo_connection_id == connection_id,
            CeligoScriptAttachment.script_celigo_id.in_(celigo_ids),
        )
    )
    rows = (await db.execute(stmt)).all()
    return [
        _SiteRow(
            attachment=attachment,
            flow_name=flow_name,
            flow_disabled=flow_disabled,
            integration_id=integration_id,
            integration_name=integration_name,
            errors_checked_at=errors_checked_at,
            step_role=step_role,
            step_adaptor_type=step_adaptor_type,
            step_record_type=step_record_type,
            step_operation=step_operation,
            step_reference_name=step_reference_name,
        )
        for (
            attachment,
            flow_name,
            flow_disabled,
            integration_id,
            errors_checked_at,
            integration_name,
            step_role,
            step_adaptor_type,
            step_record_type,
            step_operation,
            step_reference_name,
        ) in rows
    ]


async def _fetch_open_error_counts(
    db: AsyncSession, *, tenant_id: uuid.UUID, flow_step_ids: set[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not flow_step_ids:
        return {}
    stmt = (
        select(CeligoFlowError.flow_step_id, func.count())
        .where(
            CeligoFlowError.tenant_id == tenant_id,
            CeligoFlowError.flow_step_id.in_(flow_step_ids),
            celigo_error_is_open(),
        )
        .group_by(CeligoFlowError.flow_step_id)
    )
    return {flow_step_id: count for flow_step_id, count in (await db.execute(stmt)).all()}


async def _fetch_synced_at(db: AsyncSession, *, connection_id: uuid.UUID) -> datetime | None:
    return (
        await db.execute(
            select(CursorState.last_synced_at).where(
                CursorState.connection_id == connection_id,
                CursorState.object_type == _SYNC_OBJECT_TYPE,
            )
        )
    ).scalar_one_or_none()


async def _fetch_flows_total(db: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> int:
    """Every PRODUCTION flow under this connection, whether or not it has any
    script attachment -- a flow with zero attachments never shows up in
    `_fetch_sites`'s join, so `totals.flows_total` needs its own count to
    include it at all (a flow-with-sites count alone would silently pass for
    the whole account)."""
    stmt = (
        select(func.count(CeligoFlow.id))
        .select_from(CeligoFlow)
        .join(
            CeligoIntegration,
            and_(
                CeligoIntegration.id == CeligoFlow.integration_id,
                CeligoIntegration.tenant_id == tenant_id,
                celigo_integration_is_production(),
            ),
        )
        .where(CeligoFlow.tenant_id == tenant_id, CeligoFlow.celigo_connection_id == connection_id)
    )
    return (await db.execute(stmt)).scalar_one()


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _summarize_family(
    dedup_key: str,
    members: list[CeligoScript],
    site_rows: list[_SiteRow],
    open_error_counts: dict[uuid.UUID, int],
    sizes_by_id: dict[uuid.UUID, int | None],
    letters: dict[str, str],
) -> ScriptFamilySummary:
    site_types = {row.attachment.site_type or "unknown" for row in site_rows}
    kind = _kind_from_site_types(site_types)
    function_name = _pick_mode([row.attachment.function_name for row in site_rows])

    flow_ids = {row.attachment.flow_id for row in site_rows}
    integration_ids = sorted({row.integration_id for row in site_rows}, key=str)
    flow_names = sorted({row.flow_name for row in site_rows})
    sites_with_open_errors = sum(
        1
        for row in site_rows
        if row.attachment.flow_step_id is not None and open_error_counts.get(row.attachment.flow_step_id, 0) > 0
    )
    sites_unchecked = sum(1 for row in site_rows if row.errors_checked_at is None)

    original_present = any(_is_original(m) for m in members)
    name = _family_name(members)

    versions_count = len(letters)
    modifieds = [m.celigo_last_modified for m in members if m.celigo_last_modified is not None]
    sizes = [sizes_by_id[m.id] for m in members if sizes_by_id.get(m.id) is not None]

    return ScriptFamilySummary(
        dedup_key=dedup_key,
        name=name,
        kind=kind,
        function_name=function_name,
        copies_count=len(members),
        versions_count=versions_count,
        content_diverged=versions_count > 1,
        original_present=original_present,
        sites_count=len(site_rows),
        flows_count=len(flow_ids),
        integrations_count=len(integration_ids),
        integration_ids=integration_ids,
        flow_names=flow_names,
        sites_with_open_errors=sites_with_open_errors,
        sites_unchecked=sites_unchecked,
        first_modified=min(modifieds) if modifieds else None,
        last_modified=max(modifieds) if modifieds else None,
        max_size_bytes=max(sizes) if sizes else None,
        other_families_with_name=0,  # filled in by the caller, once every family's name is known
    )


def _build_member(
    script: CeligoScript,
    letters: dict[str, str],
    sites_for_script: list[_SiteRow],
    sizes_by_id: dict[uuid.UUID, int | None],
) -> ScriptFamilyMember:
    flows = {row.attachment.flow_id for row in sites_for_script}
    return ScriptFamilyMember(
        script_id=script.id,
        celigo_id=script.celigo_id,
        name=script.name,
        is_original=_is_original(script),
        version_letter=letters.get(script.content_hash) if script.content_hash is not None else None,
        content_hash=script.content_hash,
        size_bytes=sizes_by_id.get(script.id),
        celigo_last_modified=script.celigo_last_modified,
        sites_count=len(sites_for_script),
        flows_count=len(flows),
        content=script.content,
    )


def _build_versions(
    members: list[CeligoScript],
    letters: dict[str, str],
    sites_by_celigo_id: dict[str, list[_SiteRow]],
    sizes_by_id: dict[uuid.UUID, int | None],
) -> list[ScriptFamilyVersion]:
    by_hash: dict[str, list[CeligoScript]] = defaultdict(list)
    for m in members:
        if m.content_hash is not None:
            by_hash[m.content_hash].append(m)

    versions: list[ScriptFamilyVersion] = []
    for content_hash, letter in sorted(letters.items(), key=lambda kv: kv[1]):
        group = by_hash[content_hash]
        sites_count = sum(len(sites_by_celigo_id.get(m.celigo_id, [])) for m in group)
        timestamps = [m.celigo_last_modified for m in group if m.celigo_last_modified is not None]
        size_bytes = next((sizes_by_id.get(m.id) for m in group if sizes_by_id.get(m.id) is not None), None)
        versions.append(
            ScriptFamilyVersion(
                letter=letter,
                content_hash=content_hash,
                copies_count=len(group),
                sites_count=sites_count,
                first_seen=min(timestamps) if timestamps else None,
                size_bytes=size_bytes,
                holds_original=any(_is_original(m) for m in group),
            )
        )
    return versions


def _build_site(
    row: _SiteRow,
    scripts_by_id: dict[uuid.UUID, CeligoScript],
    letters: dict[str, str],
    open_error_counts: dict[uuid.UUID, int],
) -> ScriptFamilySite:
    attachment = row.attachment
    script = scripts_by_id.get(attachment.script_id) if attachment.script_id is not None else None
    content_hash = script.content_hash if script is not None else None
    version_letter = letters.get(content_hash) if content_hash is not None else None
    flow_step_id = attachment.flow_step_id
    open_error_count = open_error_counts.get(flow_step_id, 0) if flow_step_id is not None else None
    return ScriptFamilySite(
        attachment_id=attachment.id,
        script_id=attachment.script_id,
        script_celigo_id=attachment.script_celigo_id,
        version_letter=version_letter,
        integration_id=row.integration_id,
        integration_name=row.integration_name,
        flow_id=attachment.flow_id,
        flow_name=row.flow_name,
        flow_disabled=row.flow_disabled,
        flow_step_id=flow_step_id,
        step_reference_name=row.step_reference_name,
        step_role=row.step_role,
        step_adaptor_type=row.step_adaptor_type,
        step_record_type=row.step_record_type,
        step_operation=row.step_operation,
        json_path=attachment.json_path,
        function_name=attachment.function_name,
        site_type=attachment.site_type or "unknown",
        open_error_count=open_error_count,
        errors_checked_at=row.errors_checked_at,
    )


def _index_sites_by_celigo_id(site_rows: list[_SiteRow]) -> dict[str, list[_SiteRow]]:
    by_celigo_id: dict[str, list[_SiteRow]] = defaultdict(list)
    for row in site_rows:
        by_celigo_id[row.attachment.script_celigo_id].append(row)
    return by_celigo_id


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def list_script_families(
    db: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> ScriptFamiliesList:
    rows = await _fetch_production_scripts(db, tenant_id=tenant_id, connection_id=connection_id, load_content=False)
    scripts = [script for script, _ in rows]
    sizes_by_id = {script.id: size_bytes for script, size_bytes in rows}
    by_family = _group_scripts(scripts)

    all_celigo_ids = [s.celigo_id for s in scripts]
    site_rows = await _fetch_sites(db, tenant_id=tenant_id, connection_id=connection_id, celigo_ids=all_celigo_ids)
    sites_by_celigo_id = _index_sites_by_celigo_id(site_rows)

    flow_step_ids = {row.attachment.flow_step_id for row in site_rows if row.attachment.flow_step_id is not None}
    open_error_counts = await _fetch_open_error_counts(db, tenant_id=tenant_id, flow_step_ids=flow_step_ids)

    summaries: list[ScriptFamilySummary] = []
    for dedup_key, members in by_family.items():
        family_celigo_ids = {m.celigo_id for m in members}
        family_site_rows = [row for cid in family_celigo_ids for row in sites_by_celigo_id.get(cid, [])]
        letters = assign_version_letters(members)
        summaries.append(
            _summarize_family(dedup_key, members, family_site_rows, open_error_counts, sizes_by_id, letters)
        )

    name_counts = Counter(s.name for s in summaries)
    summaries = [replace(s, other_families_with_name=name_counts[s.name] - 1) for s in summaries]
    summaries.sort(key=lambda s: (-s.sites_count, -s.copies_count, s.name, s.dedup_key))

    flows_with_sites = {row.attachment.flow_id for row in site_rows}
    integrations_with_sites = {row.integration_id for row in site_rows}
    flows_total = await _fetch_flows_total(db, tenant_id=tenant_id, connection_id=connection_id)

    totals = ScriptFamilyTotals(
        scripts=len(scripts),
        families=len(summaries),
        attached_families=sum(1 for s in summaries if s.sites_count > 0),
        unattached_families=sum(1 for s in summaries if s.sites_count == 0),
        diverged_families=sum(1 for s in summaries if s.content_diverged),
        sites=len(site_rows),
        flows_with_sites=len(flows_with_sites),
        flows_total=flows_total,
        integrations_with_sites=len(integrations_with_sites),
        sites_with_open_errors=sum(s.sites_with_open_errors for s in summaries),
    )

    synced_at = await _fetch_synced_at(db, connection_id=connection_id)

    return ScriptFamiliesList(totals=totals, families=summaries, synced_at=synced_at)


async def get_script_family(
    db: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, dedup_key: str
) -> ScriptFamilyDetail | None:
    # Light scan (no `.content`): groups every production script into
    # families to find the target's own members AND every OTHER family's
    # name -- `other_families_with_name` is computed from names alone, so
    # this pass never needs content.
    light_rows = await _fetch_production_scripts(
        db, tenant_id=tenant_id, connection_id=connection_id, load_content=False
    )
    light_scripts = [script for script, _ in light_rows]
    by_family_light = _group_scripts(light_scripts)
    light_members = by_family_light.get(dedup_key)
    if light_members is None:
        return None

    # Full content, scoped to ONLY the target family's own celigo_ids.
    family_celigo_ids = [m.celigo_id for m in light_members]
    member_rows = await _fetch_scripts_by_celigo_id(
        db, tenant_id=tenant_id, connection_id=connection_id, celigo_ids=family_celigo_ids
    )
    members = [script for script, _ in member_rows]
    sizes_by_id = {script.id: size_bytes for script, size_bytes in member_rows}
    scripts_by_id = {s.id: s for s in members}

    site_rows = await _fetch_sites(db, tenant_id=tenant_id, connection_id=connection_id, celigo_ids=family_celigo_ids)
    sites_by_celigo_id = _index_sites_by_celigo_id(site_rows)

    flow_step_ids = {row.attachment.flow_step_id for row in site_rows if row.attachment.flow_step_id is not None}
    open_error_counts = await _fetch_open_error_counts(db, tenant_id=tenant_id, flow_step_ids=flow_step_ids)

    letters = assign_version_letters(members)
    summary = _summarize_family(dedup_key, members, site_rows, open_error_counts, sizes_by_id, letters)
    name_counts = Counter(_family_name(other_members) for other_members in by_family_light.values())
    summary = replace(summary, other_families_with_name=name_counts[summary.name] - 1)

    members_out = [
        _build_member(m, letters, sites_by_celigo_id.get(m.celigo_id, []), sizes_by_id)
        for m in sorted(members, key=lambda s: (s.celigo_last_modified is None, s.celigo_last_modified, s.celigo_id))
    ]
    versions_out = _build_versions(members, letters, sites_by_celigo_id, sizes_by_id)
    sites_out = sorted(
        (_build_site(row, scripts_by_id, letters, open_error_counts) for row in site_rows),
        key=lambda s: (s.integration_name or "", s.flow_name, s.json_path),
    )

    return ScriptFamilyDetail(summary=summary, members=members_out, versions=versions_out, sites=sites_out)
