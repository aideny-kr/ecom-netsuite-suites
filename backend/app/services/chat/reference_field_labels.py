"""Human-readable labels for NetSuite reference fields, for DISPLAY ONLY.

A NetSuite reference field arrives as ``{"id": "5"}`` (sometimes as a bare
``"5"``). Rendered verbatim, the write-confirmation card asks an operator to
approve ``subsidiary {"id": "5"}`` — a number whose meaning they must already
know, on the one screen whose entire purpose is informed consent before a
write lands in the ERP.

TWO PROPERTIES HOLD THIS UP, and both are easy to break by accident:

1. DISPLAY ONLY. ``proposed_fields`` is what the card renders; ``tool_input``
   is what executes, under an HMAC signature. Nothing here may touch either —
   a label leaking into the payload would post "Framework Computer UK Ltd
   (ID 5)" to NetSuite as a field VALUE. So this module returns a SEPARATE
   map and never mutates its input.

2. SERVER-SOURCED. Labels come from the same server-executed option source the
   editable slots use (``slot_option_sources``), never from the model. A
   model-supplied label could name one subsidiary while the id posts to
   another — a card that lies about what it is about to do, which is strictly
   worse than showing the bare id.

An id the server's own list does not contain is left UNLABELLED rather than
guessed at. Showing no label is honest; inventing one asserts a fact we cannot
support on a financial write path.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.services.chat.slot_option_sources import fetch_options, is_option_sourced
from app.services.chat.tools import parse_external_tool_name

logger = logging.getLogger(__name__)

__all__ = ["clear_label_cache", "resolve_reference_labels"]

# Card rendering sits on the SSE path, and the option fetch is a live SuiteQL
# round trip. One per card is a latency cost worth paying once, not on every
# write — these lists (subsidiaries) change on the order of months. Keyed by
# (connector_id, field) so one tenant's list can never label another's ids.
_TTL_SECONDS = 900
_cache: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}


def clear_label_cache() -> None:
    _cache.clear()


def _reference_id(value: Any) -> str | None:
    """The id inside a reference value, or None if this is not one.

    Accepts both shapes the model actually produces: ``{"id": "5"}`` (what
    NetSuite's REST API expects) and a bare ``"5"``/``5``. A dict without an
    ``id`` key, or any other type, is not a reference.
    """
    if isinstance(value, dict):
        raw = value.get("id")
        return str(raw) if raw is not None and not isinstance(raw, (dict, list)) else None
    if isinstance(value, bool):
        # A checkbox is not a reference; bool is an int subclass, so this must
        # be rejected before the int branch below.
        return None
    if isinstance(value, (str, int)):
        text = str(value).strip()
        return text or None
    return None


async def _options_by_id(
    field: str,
    *,
    connector_id: str,
    mutation_tool_name: str,
    tenant_id: Any,
    actor_id: Any,
    correlation_id: str,
    db: Any,
    session_id: str,
) -> dict[str, str]:
    key = (connector_id, field)
    hit = _cache.get(key)
    if hit and (time.monotonic() - hit[0]) < _TTL_SECONDS:
        return hit[1]

    options = await fetch_options(
        field,
        mutation_tool_name=mutation_tool_name,
        tenant_id=tenant_id,
        actor_id=actor_id,
        correlation_id=correlation_id,
        db=db,
        session_id=session_id,
    )
    if not options:
        # Do NOT cache an empty result: a transient MCP failure would
        # otherwise suppress labels for the whole TTL.
        return {}

    by_id = {str(o["value"]): str(o["label"]) for o in options if o.get("value") is not None}
    _cache[key] = (time.monotonic(), by_id)
    return by_id


async def resolve_reference_labels(
    fields: dict[str, Any],
    *,
    mutation_tool_name: str,
    tenant_id: Any,
    actor_id: Any,
    correlation_id: str,
    db: Any,
    session_id: str,
) -> dict[str, str]:
    """Map ``{field: "Human Name (ID n)"}`` for reference fields we can resolve.

    Returns only what it could resolve — an unknown id, a field with no option
    source, or a failed fetch each simply produce no entry. Never raises: a
    missing label is cosmetic, but an exception here would block the card and
    therefore the write.
    """
    if not fields:
        return {}

    parsed = parse_external_tool_name(mutation_tool_name)
    if not parsed:
        return {}
    connector_id = str(parsed[0])

    labels: dict[str, str] = {}
    for name, value in fields.items():
        if not is_option_sourced(name):
            continue
        ref_id = _reference_id(value)
        if ref_id is None:
            continue
        try:
            by_id = await _options_by_id(
                name,
                connector_id=connector_id,
                mutation_tool_name=mutation_tool_name,
                tenant_id=tenant_id,
                actor_id=actor_id,
                correlation_id=correlation_id,
                db=db,
                session_id=session_id,
            )
        except Exception:
            logger.warning("reference_field_labels: lookup failed for %r", name, exc_info=True)
            continue
        label = by_id.get(ref_id)
        if label:
            # The id stays visible: it is what actually posts to NetSuite, so
            # an operator approving a financial write can still tie the card
            # back to the record.
            labels[name] = f"{label} (ID {ref_id})"
    return labels
