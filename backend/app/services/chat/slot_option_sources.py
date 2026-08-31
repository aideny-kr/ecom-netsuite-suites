"""Server-side registry of option sources for editable write-confirmation slots.

A model may HINT that a write needs a human choice by including
``tool_input["ask_user"] = ["subsidiary"]`` on a write tool call (see the
mutation intercept in ``agents/base_agent.py``). The server never trusts a
model-supplied value or option list for that hint. A hinted name only becomes
an :class:`~app.services.chat.write_validator.EditableSlot` if BOTH:

1. the name exists in the record type's real metadata (verified against
   ``record_metadata_service``, elsewhere), and
2. the name is a key in ``_REGISTRY`` below — a server-side, code-reviewed
   fetch, not model output.

When both hold, the server executes the registered fetch **itself** and
builds the allow-set entirely from that tool's result. The model's
contribution is a field NAME; every VALUE a human can pick from was produced
by a server-side tool call. Adding a field is one registry line — reviewed
code, never something a model can add at runtime.

v1: ``subsidiary`` only, via ``ns_getSubsidiaries``. location/department/class
are deferred to a future SuiteQL-backed entry — see
``docs/superpowers/plans/2026-08-19-agentic-netsuite-write-loop.md``.

The real response shape of ``ns_getSubsidiaries`` has not been controller-
verified live (unlike ``ns_getRecordTypeMetadata``) — the parser below is
deliberately tolerant of the plausible id/name key variants and fails closed
(returns ``[]``) on anything it doesn't recognise, following the same
"unknown, never invented" discipline as ``record_metadata_service.py``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.services.chat.record_metadata_service import RecordMetadata, coerce_netsuite_bool
from app.services.chat.tools import _make_ext_tool_name, execute_tool_call, parse_external_tool_name
from app.services.chat.write_validator import EditableSlot

logger = logging.getLogger(__name__)

_OptionFetch = Callable[..., Awaitable[list[dict[str, Any]]]]


# Every column needed to decide assignability. The filtering happens in Python
# rather than in a WHERE clause so the rule is visible and unit-testable next
# to the reasoning that justifies it, and so a NULL flag cannot silently drop a
# row the way `WHERE iselimination = 'F'` would.
_SUBSIDIARY_SQL = "SELECT id, name, iselimination, isinactive FROM subsidiary ORDER BY name"


def _is_assignable(row: dict[str, Any]) -> bool:
    """True unless we AFFIRMATIVELY know a record cannot be assigned here.

    An elimination subsidiary is an intercompany bookkeeping construct and an
    inactive one is retired; neither accepts a customer or vendor. But the
    exclusion is deliberately one-directional: wrongly excluding a valid
    subsidiary BLOCKS a legitimate write, while wrongly including one fails at
    NetSuite and recovers through the repair loop. So an absent or null flag
    keeps the subsidiary — same asymmetry `required_field_registry` is built
    around, pointing the same way.
    """
    return not coerce_netsuite_bool(row.get("iselimination")) and not coerce_netsuite_bool(row.get("isinactive"))


def _option(value: Any, label: Any) -> dict[str, Any] | None:
    """Normalize one row to {value, label}, or None if it must not be offered.

    A NEGATIVE id is NetSuite's marker for a consolidated roll-up — stated in
    ns_getSubsidiaries' own tool description ("Consolidated subsidiaries have a
    negative ID"), and confirmed live: that tool returned 6 entries for account
    6738075 while the `subsidiary` table holds 5. The extra one is a reporting
    artifact with no record behind it, so nothing can be assigned to it.
    """
    if value is None or label is None:
        return None
    try:
        if int(str(value).strip()) < 0:
            return None
    except (TypeError, ValueError):
        # A non-numeric id is not a roll-up; leave it alone rather than
        # guessing it is invalid.
        pass
    return {"value": str(value), "label": str(label)}


async def _subsidiaries_via_suiteql(
    *,
    connector_id: Any,
    tenant_id: Any,
    actor_id: Any,
    correlation_id: str,
    db: Any,
    session_id: str,
) -> list[dict[str, Any]]:
    """Preferred source: the subsidiary table carries the flags that decide
    assignability, which ns_getSubsidiaries (built for the REPORT tool) does
    not return. Returns [] on any failure so the caller can fall back."""
    tool = _make_ext_tool_name(connector_id, "ns_runCustomSuiteQL")
    try:
        raw = await execute_tool_call(
            tool_name=tool,
            tool_input={"sqlQuery": _SUBSIDIARY_SQL},
            tenant_id=tenant_id,
            actor_id=actor_id,
            correlation_id=correlation_id,
            db=db,
            session_id=session_id,
        )
        data = json.loads(raw)
    except Exception:
        logger.info("slot_option_sources: subsidiary SuiteQL unavailable, falling back", exc_info=True)
        return []

    if not isinstance(data, dict) or data.get("error"):
        return []
    rows = data.get("data")
    if not isinstance(rows, list):
        return []

    options: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not _is_assignable(row):
            continue
        option = _option(row.get("id"), row.get("name"))
        if option:
            options.append(option)
    return options


async def _subsidiaries_via_report_tool(
    *,
    connector_id: Any,
    tenant_id: Any,
    actor_id: Any,
    correlation_id: str,
    db: Any,
    session_id: str,
) -> list[dict[str, Any]]:
    """Fallback: ns_getSubsidiaries needs no search permission, so it works on
    roles SuiteQL does not. It cannot distinguish an elimination subsidiary —
    losing the dropdown entirely on such a role would be a worse regression
    than a slightly coarser list, since a bad pick still fails safe at
    NetSuite."""
    tool = _make_ext_tool_name(connector_id, "ns_getSubsidiaries")
    try:
        raw = await execute_tool_call(
            tool_name=tool,
            tool_input={},
            tenant_id=tenant_id,
            actor_id=actor_id,
            correlation_id=correlation_id,
            db=db,
            session_id=session_id,
        )
        data = json.loads(raw)
    except Exception:
        logger.warning("slot_option_sources: subsidiary fetch failed", exc_info=True)
        return []

    if not isinstance(data, dict) or data.get("error"):
        return []

    subsidiaries = data.get("subsidiaries")
    if not isinstance(subsidiaries, list):
        return []

    options: list[dict[str, Any]] = []
    for entry in subsidiaries:
        if not isinstance(entry, dict):
            continue
        option = _option(entry.get("id", entry.get("internalId")), entry.get("name", entry.get("label")))
        if option:
            options.append(option)
    return options


async def _fetch_subsidiaries(
    *,
    mutation_tool_name: str,
    tenant_id: Any,
    actor_id: Any,
    correlation_id: str,
    db: Any,
    session_id: str,
) -> list[dict[str, Any]]:
    """Subsidiaries a human may actually be offered, on the SAME connector as
    the write tool that prompted the ask_user hint.

    Never raises — a fetch failure or malformed response means "no slot",
    reported to the model by the caller, not a 500.
    """
    parsed = parse_external_tool_name(mutation_tool_name)
    if not parsed:
        return []
    connector_id = parsed[0]
    kwargs = {
        "connector_id": connector_id,
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "correlation_id": correlation_id,
        "db": db,
        "session_id": session_id,
    }
    return await _subsidiaries_via_suiteql(**kwargs) or await _subsidiaries_via_report_tool(**kwargs)


# Field name -> server-executed option fetch. This IS the authorization
# surface: a name not present here can never become an editable slot no
# matter what the model claims about it.
_REGISTRY: dict[str, _OptionFetch] = {
    "subsidiary": _fetch_subsidiaries,
}


def is_option_sourced(field_name: str) -> bool:
    """True if *field_name* has a server-side, code-reviewed option fetch."""
    return field_name in _REGISTRY


async def fetch_options(
    field_name: str,
    *,
    mutation_tool_name: str,
    tenant_id: Any,
    actor_id: Any,
    correlation_id: str,
    db: Any,
    session_id: str,
) -> list[dict[str, Any]]:
    """Run the registered fetch for *field_name* and return its options.

    Returns ``[]`` for an unregistered name, a fetch failure, a malformed
    response, OR a fetch that legitimately yields zero options. Callers MUST
    treat every empty result the same way — decline to declare a slot and
    tell the model, per the design (an empty allow-set that still becomes a
    slot is a fail-closed dead end at merge time: better to surface it as
    "no options available" to the model than to render an unusable dropdown).
    """
    fetch = _REGISTRY.get(field_name)
    if fetch is None:
        return []
    return await fetch(
        mutation_tool_name=mutation_tool_name,
        tenant_id=tenant_id,
        actor_id=actor_id,
        correlation_id=correlation_id,
        db=db,
        session_id=session_id,
    )


async def resolve_ask_user_slots(
    hint: Any,
    *,
    metadata: RecordMetadata | None,
    mutation_tool_name: str,
    tenant_id: Any,
    actor_id: Any,
    correlation_id: str,
    db: Any,
    session_id: str,
    already_declared: list[str] | None = None,
) -> tuple[list[EditableSlot], list[dict[str, str]]]:
    """Resolve a model-supplied ``ask_user`` hint into server-verified
    editable slots.

    This is the ENTIRE (C) server-authority boundary in one place: *hint* is
    untrusted model output — a list of field NAMES the model claims it needs
    a human to choose. A name only becomes an :class:`EditableSlot` if it
    passes BOTH checks (exists in *metadata*'s real properties, AND is a key
    in the option-source ``_REGISTRY``) and the registered fetch — executed
    by the SERVER, never the model — returns at least one option. Every
    other outcome (malformed hint shape, unknown field, unregistered field,
    zero options, already covered by a server-derived slot) produces NO
    slot; the second return value names why, so the caller can tell the
    model rather than silently drop its request.

    Never raises: a malformed *hint* (not a list, non-string entries) is
    handled by rejecting the offending entries, not by raising into the
    mutation intercept.
    """
    if not isinstance(hint, list):
        return [], []

    declared = set(already_declared or [])
    seen: set[str] = set()
    slots: list[EditableSlot] = []
    rejected: list[dict[str, str]] = []

    for name in hint:
        if not isinstance(name, str) or not name:
            rejected.append({"name": str(name), "reason": "ask_user entries must be non-empty field name strings."})
            continue
        if name in seen or name in declared:
            continue
        seen.add(name)

        spec = metadata.spec_for(name) if metadata is not None else None
        if spec is None:
            rejected.append(
                {"name": name, "reason": f"'{name}' is not a recognized field on this record type's metadata."}
            )
            continue
        if not is_option_sourced(name):
            rejected.append({"name": name, "reason": f"'{name}' has no server-side option source registered for it."})
            continue

        options = await fetch_options(
            name,
            mutation_tool_name=mutation_tool_name,
            tenant_id=tenant_id,
            actor_id=actor_id,
            correlation_id=correlation_id,
            db=db,
            session_id=session_id,
        )
        if not options:
            rejected.append({"name": name, "reason": f"no options are currently available for '{name}'."})
            continue

        slots.append(EditableSlot(name=name, label=spec.label, type=spec.type, allowed=options))

    return slots, rejected
