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

from app.services.chat.tools import _make_ext_tool_name, execute_tool_call, parse_external_tool_name

logger = logging.getLogger(__name__)

_OptionFetch = Callable[..., Awaitable[list[dict[str, Any]]]]


async def _fetch_subsidiaries(
    *,
    mutation_tool_name: str,
    tenant_id: Any,
    actor_id: Any,
    correlation_id: str,
    db: Any,
    session_id: str,
) -> list[dict[str, Any]]:
    """Server-executes ns_getSubsidiaries on the SAME connector as the write
    tool that prompted the ask_user hint, and returns [{value, label}, ...].

    Never raises — a fetch failure or malformed response means "no slot",
    reported to the model by the caller, not a 500.
    """
    parsed = parse_external_tool_name(mutation_tool_name)
    if not parsed:
        return []
    connector_id = parsed[0]
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
        value = entry.get("id", entry.get("internalId"))
        label = entry.get("name", entry.get("label"))
        if value is None or label is None:
            continue
        options.append({"value": str(value), "label": str(label)})
    return options


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
