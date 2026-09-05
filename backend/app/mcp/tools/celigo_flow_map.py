"""Four read-only ``celigo.*`` chat tools over the synced flow map (spec
``docs/superpowers/specs/2026-09-04-celigo-chat-access.md`` §3-§5, §8-§10,
task 3). Every ``execute()`` below is a thin adapter over
``app.services.celigo.read_queries``/``run_state``: the two resolvers it
calls (``resolve_production_integration``/``resolve_production_flow``) are
the only new queries this family needed, and they live in
``read_queries.py``, not here -- this module never runs a new query against
a ``celigo_*`` table itself.

One envelope shape, always every key present::

    {"columns": [str], "rows": [[cell, ...]], "row_count": int,
     "query": str, "truncated": bool, "caveats": [str]}

``caveats`` is the honesty channel (spec §8) -- plain sentences, the FIRST
always the snapshot line. Never put a caveat under an ``error`` key:
``governance.create_audit_payload`` treats a truthy ``error`` as a failed
call, and a caveat is not a failure. Every cell is a JSON scalar
(str/int/float/bool/None); every datetime is rendered ISO 8601 before it
reaches a row.

N2 (spec §1, "script bodies stay out"): no function here selects, or is
handed, a script's ``content``/``content_hash`` -- ``read_queries.py``'s own
N2 docstring covers the query layer; ``test_celigo_chat_tools.py``'s shape
test walks every envelope this module produces AS JSON and asserts neither
key ever appears -- the second, independent half of "enforced by shape, not
a guarded parameter".

Execute skeleton (mirrors ``mcp/tools/data_sample.py``), same order in every
tool below:

1. Validate params against an explicit allowlist -- unknown param, wrong
   type, or ``limit`` out of range raises ``ValueError`` (the ONLY raise;
   a caller mistake, not a data problem).
2. Read ``db``/``tenant_id`` off ``context``; no ``db`` -> empty envelope.
3. Gate on the ``celigo`` feature flag, then the tenant's Celigo connection.
4. Gate on the sync cursor (``read_queries.sync_status``) -- never synced
   means empty rows regardless of what the tables hold.
5. Query via ``read_queries``, build rows, compute caveats, return.
6. Any exception from steps 3-5 is logged (``logger.warning``, never
   formatting a message/sample_message field into the log line) and turned
   into an empty envelope with a generic caveat -- a tool failure must never
   surface a raw traceback or a fabricated zero to the model.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from app.services import feature_flag_service
from app.services.celigo import read_queries, run_state
from app.services.celigo.topology import adaptor_family

logger = logging.getLogger(__name__)

_SAMPLE_MESSAGE_MAX = 300


# ---------------------------------------------------------------------------
# Envelope helpers -- shared by all four tools.
# ---------------------------------------------------------------------------


def _envelope(columns: tuple[str, ...], rows: list[list], query: str, *, truncated: bool = False, caveats=None) -> dict:
    return {
        "columns": list(columns),
        "rows": [list(r) for r in rows],
        "row_count": len(rows),
        "query": query,
        "truncated": truncated,
        "caveats": list(caveats) if caveats else [],
    }


def _empty(columns: tuple[str, ...], caveat: str) -> dict:
    """A gated/failed call: every key present, no rows, one caveat naming
    why. ``query`` is empty -- there is nothing to describe, and the caveat
    already says what happened."""
    return _envelope(columns, [], "", truncated=False, caveats=[caveat])


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _as_uuid(value) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _errors_checked_cell(checked_at: datetime | None) -> str:
    return f"verified {_iso(checked_at)}" if checked_at is not None else "not fully checked"


def _snapshot_caveat(last_synced_at: datetime) -> str:
    return f"Snapshot of production flows as of {_iso(last_synced_at)}."


async def _gate(context: dict, columns: tuple[str, ...]):
    """Steps 2-4 of the execute skeleton, shared by every tool below: ``db``
    presence, the ``celigo`` feature flag, the tenant's Celigo connection,
    and the sync cursor. Returns ``(None, db, tenant_id, last_synced_at)``
    when every gate passes, or ``(envelope, None, None, None)`` for the
    caller to return immediately -- callers never need to build the empty
    envelope themselves for these four conditions."""
    db = context.get("db")
    if db is None:
        return _empty(columns, "No database session — nothing was read."), None, None, None

    tenant_id = _as_uuid(context.get("tenant_id"))

    if not await feature_flag_service.is_enabled(db, tenant_id, "celigo"):
        return _empty(columns, "Celigo is turned off for this workspace."), None, None, None

    connection = await read_queries._get_celigo_connection(db, tenant_id)
    if connection is None:
        return _empty(columns, "This workspace has no Celigo connection."), None, None, None

    status = await read_queries.sync_status(db, tenant_id=tenant_id)
    if status.last_synced_at is None:
        return _empty(columns, "This workspace has never completed a Celigo sync."), None, None, None

    return None, db, tenant_id, status.last_synced_at


# ---------------------------------------------------------------------------
# Param validation -- explicit allowlists, ValueError the only raise.
# ---------------------------------------------------------------------------


def _reject_unknown(tool_name: str, params: dict, allowed: set[str]) -> None:
    unknown = set(params) - allowed
    if unknown:
        raise ValueError(f"Unknown parameter(s) for {tool_name}: {sorted(unknown)}")


def _validate_str(params: dict, name: str, *, required: bool = False) -> str | None:
    if name not in params or params[name] is None:
        if required:
            raise ValueError(f"{name} is required")
        return None
    value = params[name]
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _validate_bool(params: dict, name: str, *, default: bool) -> bool:
    if name not in params:
        return default
    value = params[name]
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _validate_limit(params: dict, *, default: int, maximum: int) -> int:
    if "limit" not in params:
        return default
    value = params["limit"]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("limit must be an integer")
    if not (1 <= value <= maximum):
        raise ValueError(f"limit must be between 1 and {maximum}")
    return value


def _validate_integrations_params(params: dict) -> None:
    _reject_unknown("celigo.integrations", params, set())


def _validate_flows_params(params: dict) -> dict:
    allowed = {"integration", "only_open_errors", "only_stalled", "limit"}
    _reject_unknown("celigo.flows", params, allowed)
    return {
        "integration": _validate_str(params, "integration"),
        "only_open_errors": _validate_bool(params, "only_open_errors", default=False),
        "only_stalled": _validate_bool(params, "only_stalled", default=False),
        "limit": _validate_limit(params, default=50, maximum=200),
    }


def _validate_flow_steps_params(params: dict) -> dict:
    _reject_unknown("celigo.flow_steps", params, {"flow"})
    return {"flow": _validate_str(params, "flow", required=True)}


def _validate_flow_errors_params(params: dict) -> dict:
    allowed = {"flow", "status", "limit"}
    _reject_unknown("celigo.flow_errors", params, allowed)
    status = params.get("status", "open")
    if status not in ("open", "resolved"):
        raise ValueError("status must be 'open' or 'resolved'")
    return {
        "flow": _validate_str(params, "flow"),
        "status": status,
        "limit": _validate_limit(params, default=25, maximum=50),
    }


# ---------------------------------------------------------------------------
# celigo.integrations
# ---------------------------------------------------------------------------

_INTEGRATIONS_COLUMNS = (
    "integration",
    "flows",
    "scheduled",
    "on_demand",
    "paused",
    "open_errors",
    "root_causes",
    "errors_checked",
    "last_run",
    "modified_in_celigo",
)


async def execute_integrations(params: dict, **kwargs) -> dict:
    _validate_integrations_params(params)
    context: dict = kwargs.get("context", {})
    columns = _INTEGRATIONS_COLUMNS
    try:
        gate_envelope, db, tenant_id, last_synced_at = await _gate(context, columns)
        if gate_envelope is not None:
            return gate_envelope

        summaries = await read_queries.integration_summaries(db, tenant_id=tenant_id)
        rows = [
            [
                s.name,
                s.flow_count,
                s.scheduled_count,
                s.on_demand_count,
                s.paused_count,
                s.error_count,
                s.signature_count,
                _errors_checked_cell(s.errors_checked_at),
                _iso(s.last_run_at),
                _iso(s.celigo_last_modified),
            ]
            for s in summaries
        ]

        caveats = [_snapshot_caveat(last_synced_at)]
        unchecked = sum(1 for s in summaries if s.errors_checked_at is None)
        if unchecked:
            caveats.append(
                f"Error counts for {unchecked} integration(s) are not fully checked — treat their zeros as unknown."
            )

        query = f"{len(rows)} production integration(s)." if rows else "No production integrations synced yet."
        return _envelope(columns, rows, query, caveats=caveats)
    except Exception:
        logger.warning("celigo tool failed", exc_info=True)
        return _empty(columns, "Celigo flow map could not be read.")


# ---------------------------------------------------------------------------
# celigo.flows
# ---------------------------------------------------------------------------

_FLOWS_COLUMNS = (
    "integration",
    "flow",
    "state",
    "schedule",
    "timezone",
    "last_run",
    "missed_runs",
    "steps",
    "routers",
    "branches",
    "lookups",
    "open_errors",
    "root_causes",
    "errors_checked",
)


def _schedule_label(schedule: object) -> str | None:
    """``run_state.parse_schedule(...).label`` for a parsed cron, the literal
    string ``"on demand"`` for no schedule, or the raw value verbatim when
    Celigo sent a shape this tool cannot parse -- never guessed at, same
    discipline as ``parse_schedule`` itself."""
    parsed = run_state.parse_schedule(schedule)
    if parsed.kind == "cron":
        return parsed.label
    if parsed.kind == "on_demand":
        return "on demand"
    return parsed.raw


async def execute_flows(params: dict, **kwargs) -> dict:
    validated = _validate_flows_params(params)
    context: dict = kwargs.get("context", {})
    columns = _FLOWS_COLUMNS
    try:
        gate_envelope, db, tenant_id, last_synced_at = await _gate(context, columns)
        if gate_envelope is not None:
            return gate_envelope

        caveats = [_snapshot_caveat(last_synced_at)]
        integration_key = validated["integration"]

        # (integration name, FlowSummary) pairs -- either every flow under
        # ONE resolved integration, or every flow across every production
        # integration when *integration* is omitted. Reuses `flow_summaries`
        # per integration rather than writing a tenant-wide flow query --
        # the brief is explicit this family adds no new GROUP BY.
        if integration_key is not None:
            match = await read_queries.resolve_production_integration(db, tenant_id=tenant_id, key=integration_key)
            if match is None:
                caveat = f"No Celigo integration matches '{integration_key}'."
                return _envelope(columns, [], caveat, caveats=[*caveats, caveat])
            if isinstance(match, list):
                names = ", ".join(m.name for m in match)
                caveat = f"'{integration_key}' matches {len(match)} integrations: {names}."
                return _envelope(columns, [], "Ambiguous integration.", caveats=[*caveats, caveat])
            flows = await read_queries.flow_summaries(db, tenant_id=tenant_id, integration_id=_as_uuid(match.id))
            named_flows = [(match.name, f) for f in flows]
        else:
            integrations = await read_queries.integration_summaries(db, tenant_id=tenant_id)
            named_flows = []
            for integ in integrations:
                flows = await read_queries.flow_summaries(db, tenant_id=tenant_id, integration_id=_as_uuid(integ.id))
                named_flows.extend((integ.name, f) for f in flows)

        built = []
        for integ_name, f in named_flows:
            rs = run_state.run_state(
                schedule=f.schedule, disabled=f.disabled, last_executed_at=f.last_executed_at, as_of=last_synced_at
            )
            if validated["only_open_errors"] and f.error_count <= 0:
                continue
            if validated["only_stalled"] and rs.state != "stalled":
                continue
            built.append(
                [
                    integ_name,
                    f.name,
                    rs.state,
                    _schedule_label(f.schedule),
                    f.timezone,
                    _iso(f.last_executed_at),
                    rs.missed_runs,
                    f.step_count,
                    f.router_count,
                    f.branch_count,
                    f.lookup_count,
                    f.error_count,
                    f.signature_count,
                    _errors_checked_cell(f.errors_checked_at),
                ]
            )

        total = len(built)
        limit = validated["limit"]
        truncated = total > limit
        page = built[:limit]

        # Counted over what is actually RETURNED (post-truncation) -- a
        # caveat about hidden rows would double up with "Showing N of M."
        unchecked = sum(1 for row in page if row[13] == "not fully checked")
        unknown_state = sum(1 for row in page if row[2] == "unknown")
        if unchecked:
            caveats.append(
                f"Error counts for {unchecked} flow(s) are not fully checked — treat their zeros as unknown."
            )
        if unknown_state:
            caveats.append(f"{unknown_state} flow(s) have a schedule this tool cannot judge.")

        query = f"{total} matching flow(s)." if total else "No matching flows."
        if truncated:
            query += f" Showing {len(page)} of {total}."

        return _envelope(columns, page, query, truncated=truncated, caveats=caveats)
    except Exception:
        logger.warning("celigo tool failed", exc_info=True)
        return _empty(columns, "Celigo flow map could not be read.")


# ---------------------------------------------------------------------------
# celigo.flow_steps
# ---------------------------------------------------------------------------

_FLOW_STEPS_COLUMNS = (
    "sequence",
    "kind",
    "name",
    "adaptor",
    "branch",
    "operation",
    "record_type",
    "open_errors",
    "scripts",
    "script_sites",
)

_KIND_WORD = {"source": "Source", "lookup": "Lookup", "destination": "Destination"}


def _fallback_step_title(
    kind: str,
    adaptor_type: str | None,
    record_type: str | None,
    operation: str | None,
    search_id: str | None,
) -> str:
    """Port of ``frontend/src/components/celigo/shared.tsx``'s
    ``fallbackStepTitle`` -- a step's own Celigo name is only synced once
    Phase D backfilled it (migration 097), and several adaptor families
    (HTTP/AS2/FTP/RDBMS/REST) never carry one at all. Every title here is
    honestly derived from what the step DOES, never invented; keep the two
    in step, same discipline ``run_state.py`` already applies to
    ``schedule.ts``."""
    family = adaptor_family(adaptor_type)
    if family == "NetSuite":
        if kind == "destination" and operation and record_type:
            return f"{operation} {record_type}"
        if kind == "lookup" and search_id:
            return f"lookup {record_type or 'record'} · search {search_id}"
    if not family:
        return f"{_KIND_WORD[kind]} · adaptor not synced"
    kind_word = "export" if kind == "source" else ("lookup" if kind == "lookup" else "destination")
    return f"{family} {kind_word} · name not synced"


def _step_name(step) -> str:
    return step.reference_name or _fallback_step_title(
        step.kind, step.adaptor_type, step.record_type, step.operation, step.search_id
    )


def _join_scripts(attachments) -> tuple[str, str]:
    names = "; ".join(a.script_name or a.script_celigo_id for a in attachments)
    sites = "; ".join(a.json_path for a in attachments)
    return names, sites


def _router_row(seq: int, router: dict) -> list:
    rid = router.get("id")
    name = router.get("name") or (f"Router {rid}" if rid else None) or "(unnamed router)"
    return [seq, "router", name, None, None, None, None, None, "", ""]


def _flow_step_rows(detail: "read_queries.FlowDetail") -> list[list]:
    """Steps AND routers, in sequence order: a router row is emitted right
    before the first step whose ``router_id`` names it (routers[] carries no
    ordering of its own relative to the flat step list; this is the one
    deterministic anchor a router has to the sequence). A router with zero
    synced branch steps -- Celigo allows this -- is appended after every
    step, in declared order, rather than silently dropped.

    Router-level script attachments (``flow_detail``'s
    ``unassigned_attachments`` -- a hook on the router itself, not on any
    step) are NOT attributed to a specific router here: the attachment table
    carries no ``router_id`` column, only a ``json_path`` string, and
    guessing which router a hook belongs to when a flow has more than one
    would be exactly the kind of invented fact this module's docstring (and
    ``run_state``'s) refuses to produce elsewhere. Router rows' ``scripts``/
    ``script_sites`` are therefore always empty; every step-level attachment
    is unaffected and still shown on its own step."""
    router_by_id = {r["id"]: r for r in detail.routers if r.get("id") is not None}
    emitted: set[str] = set()
    rows: list[list] = []
    seq = 1
    for step in detail.steps:
        router_id = step.router_id
        if router_id is not None and router_id in router_by_id and router_id not in emitted:
            rows.append(_router_row(seq, router_by_id[router_id]))
            seq += 1
            emitted.add(router_id)
        scripts, sites = _join_scripts(step.attachments)
        rows.append(
            [
                seq,
                step.kind,
                _step_name(step),
                step.adaptor_type,
                step.branch_key,
                step.operation,
                step.record_type,
                step.error_count,
                scripts,
                sites,
            ]
        )
        seq += 1
    for router_id, router in router_by_id.items():
        if router_id not in emitted:
            rows.append(_router_row(seq, router))
            seq += 1
    return rows


async def execute_flow_steps(params: dict, **kwargs) -> dict:
    validated = _validate_flow_steps_params(params)
    context: dict = kwargs.get("context", {})
    columns = _FLOW_STEPS_COLUMNS
    try:
        gate_envelope, db, tenant_id, last_synced_at = await _gate(context, columns)
        if gate_envelope is not None:
            return gate_envelope

        caveats = [_snapshot_caveat(last_synced_at)]
        flow_key = validated["flow"]

        refs = await read_queries.resolve_production_flow(db, tenant_id=tenant_id, key=flow_key)
        if not refs:
            caveat = f"No Celigo flow matches '{flow_key}'."
            return _envelope(columns, [], caveat, caveats=[*caveats, caveat])
        if len(refs) > 1:
            names = ", ".join(f"{r.name} ({r.integration_name})" for r in refs)
            caveat = f"'{flow_key}' matches {len(refs)} flows: {names}."
            return _envelope(columns, [], "Ambiguous flow.", caveats=[*caveats, caveat])

        ref = refs[0]
        detail = await read_queries.flow_detail(db, tenant_id=tenant_id, flow_id=_as_uuid(ref.id))
        if detail is None:
            # Resolved a moment ago, gone now -- report the same honest
            # "no match" shape as above rather than crash on a race.
            caveat = f"No Celigo flow matches '{flow_key}'."
            return _envelope(columns, [], caveat, caveats=[*caveats, caveat])

        rows = _flow_step_rows(detail)
        query = (
            f"{len(rows)} step(s)/router(s) in flow '{detail.name}'."
            if rows
            else f"No steps synced for flow '{detail.name}'."
        )
        return _envelope(columns, rows, query, caveats=caveats)
    except Exception:
        logger.warning("celigo tool failed", exc_info=True)
        return _empty(columns, "Celigo flow map could not be read.")


# ---------------------------------------------------------------------------
# celigo.flow_errors
# ---------------------------------------------------------------------------

_FLOW_ERRORS_COLUMNS = (
    "flow",
    "source",
    "code",
    "occurrences",
    "first_seen",
    "last_seen",
    "sample_message",
    "trace_keys",
    "steps",
    "purge_at",
)


def _group_source_code_message(group) -> tuple[str | None, str | None, str | None]:
    """A group's display source/code/sample message -- from its signature
    when it has one (the normal case), else from its first raw error (the
    legacy/unfingerprinted bucket ``flow_error_groups`` already collapses
    every signature-less row into, per that function's own docstring --
    this reads that existing grouping, it does not invent a new one)."""
    if group.signature is not None:
        return group.signature.source, group.signature.code, group.signature.sample_message
    first = group.errors[0] if group.errors else None
    return (first.source if first else None), (first.code if first else None), (first.message if first else None)


def _cap_message(message: str | None) -> str | None:
    """PII bound (spec §9): one sample message per group, never more than
    300 chars -- the transcript/model-reasoning sink this tool is allowed to
    carry."""
    return message[:_SAMPLE_MESSAGE_MAX] if message is not None else None


async def execute_flow_errors(params: dict, **kwargs) -> dict:
    validated = _validate_flow_errors_params(params)
    context: dict = kwargs.get("context", {})
    columns = _FLOW_ERRORS_COLUMNS
    try:
        gate_envelope, db, tenant_id, last_synced_at = await _gate(context, columns)
        if gate_envelope is not None:
            return gate_envelope

        caveats = [_snapshot_caveat(last_synced_at)]
        status = validated["status"]
        limit = validated["limit"]
        flow_key = validated["flow"]

        single_ref = None
        if flow_key is not None:
            refs = await read_queries.resolve_production_flow(db, tenant_id=tenant_id, key=flow_key)
            if not refs:
                caveat = f"No Celigo flow matches '{flow_key}'."
                return _envelope(columns, [], caveat, caveats=[*caveats, caveat])
            if len(refs) > 1:
                names = ", ".join(f"{r.name} ({r.integration_name})" for r in refs)
                caveat = f"'{flow_key}' matches {len(refs)} flows: {names}."
                return _envelope(columns, [], "Ambiguous flow.", caveats=[*caveats, caveat])
            single_ref = refs[0]
            groups_result = await read_queries.flow_error_groups(
                db, tenant_id=tenant_id, flow_id=_as_uuid(single_ref.id), status=status
            )
            tagged = [(single_ref.name, g) for g in groups_result.groups]
        else:
            # Tenant-wide (spec §3 "flow_errors takes one flow_id ... iterate
            # the tenant's production flows and merge groups"): reuses
            # `integration_summaries` -> `flow_summaries` (already extracted,
            # already tested) rather than a new tenant-wide GROUP BY.
            integrations = await read_queries.integration_summaries(db, tenant_id=tenant_id)
            tagged = []
            for integ in integrations:
                flows = await read_queries.flow_summaries(db, tenant_id=tenant_id, integration_id=_as_uuid(integ.id))
                for f in flows:
                    groups_result = await read_queries.flow_error_groups(
                        db, tenant_id=tenant_id, flow_id=_as_uuid(f.id), status=status
                    )
                    tagged.extend((f.name, g) for g in groups_result.groups)
            tagged.sort(key=lambda pair: pair[1].count, reverse=True)

        total = len(tagged)
        truncated = total > limit
        page = tagged[:limit]

        rows = []
        for flow_name, g in page:
            source, code, message = _group_source_code_message(g)
            rows.append(
                [
                    flow_name,
                    source,
                    code,
                    g.count,
                    _iso(g.first_seen_at),
                    _iso(g.last_seen_at),
                    _cap_message(message),
                    len(g.trace_keys),
                    len(g.step_ids),
                    _iso(g.purge_at),
                ]
            )

        if rows:
            query = f"{total} root-cause group(s)."
            if truncated:
                query += f" Showing {len(rows)} of {total}."
        elif single_ref is not None and single_ref.errors_checked_at is not None:
            query = f"No {status} errors as of the snapshot; verified {_iso(single_ref.errors_checked_at)}."
        elif single_ref is not None:
            query = f"No {status} errors as of the snapshot; not fully checked."
            caveats.append("Error counts for 1 flow(s) are not fully checked — treat their zeros as unknown.")
        else:
            query = f"No {status} errors as of the snapshot."

        return _envelope(columns, rows, query, truncated=truncated, caveats=caveats)
    except Exception:
        logger.warning("celigo tool failed", exc_info=True)
        return _empty(columns, "Celigo flow map could not be read.")
