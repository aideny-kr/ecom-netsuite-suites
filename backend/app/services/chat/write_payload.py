"""Canonical parsing of MCP write tool_input into header fields + lines.

Every NetSuite write payload enters the system through exactly one function so
a new MCP tool schema cannot silently produce an empty confirmation card. The
Oracle NetSuite MCP sends ``data`` as a JSON *string*; older/other schemas send
``body`` as a dict. Both — and dict-valued ``data`` — normalize here.

Two payload keys are never both trusted at once: if MORE THAN ONE of
``_PAYLOAD_KEYS`` coerces to a dict, ``normalize_write_payload`` raises
``PayloadParseError`` rather than picking one — see its docstring for why
(T2 gate finding B / architect ruling). A single coerced key, even an empty
dict, is never ambiguous and is always accepted.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

# Keys that hold the record payload. "Precedence order" here means
# fall-through past a PRESENT-BUT-UNCOERCIBLE key only (e.g. `data: None`
# falls through to `body`) — it is not a tie-breaker between two keys that
# both coerce. Two coerced keys are refused; see the module docstring.
_PAYLOAD_KEYS = ("data", "body")

# Sublist keys that carry transaction lines. NetSuite uses several names
# depending on record type; all are treated as line collections.
_LINE_KEYS = ("line", "lines", "item", "items", "expense", "apply")


class PayloadParseError(ValueError):
    """The tool input carried no parseable record payload."""


class NormalizedPayload(BaseModel):
    fields: dict[str, Any]
    lines: list[dict[str, Any]]
    record_id: str | None = None
    # The raw, UNSPLIT record (fields + line-sublists together, exactly as
    # read) and which `tool_input` key it came from ("data" or "body").
    # `normalize_write_payload()` always populates both — a merge that wants
    # to add a field without losing line items has to merge into `record`
    # and write back under `payload_key`, not reassemble `fields`/`lines`
    # under a guessed key (`.lines` alone has no way back to its original
    # sublist name — `line`/`item`/`expense`/...). The defaults below exist
    # only so existing direct constructions elsewhere (validator/invariants
    # tests, which only read `.fields`/`.lines`) don't have to pass them.
    record: dict[str, Any] = {}
    payload_key: str = ""


def _coerce(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PayloadParseError(f"payload is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise PayloadParseError("payload JSON is not an object")
        return parsed
    return None


def normalize_write_payload(tool_input: dict[str, Any]) -> NormalizedPayload:
    """Return the canonical payload, or raise :class:`PayloadParseError`.

    Architect ruling (T2 gate finding B): scan EVERY key in ``_PAYLOAD_KEYS``
    (not just up to the first that coerces) and refuse — raise, never guess
    — when more than one coerces to a dict. ``execute_tool_call`` sends
    ``tool_input`` to the external MCP verbatim (no key filtering), and
    which key the remote reads is unknowable from our side (the module
    docstring above: Oracle's MCP reads ``data``, older/other schemas read
    ``body``) — so no precedence rule, "prefer non-empty" included, can ever
    make the confirmation card equal what executes when two keys coerce; it
    only picks which half of the ambiguity the card shows. A dual-populated
    input can only come from LLM hallucination (no producer in this repo
    emits one — see the grep evidence in the T2 finding), and the system
    already has the designed absorber for that: ``PayloadParseError`` here
    is caught by the mutation intercept (``base_agent.py``) and fed back to
    the model as a structured error for the bounded write-repair loop — the
    human never sees the ambiguous card. This is a change from the old
    break-on-first-match scan: a present-but-uncoercible sibling key
    (``None``/``""``/non-dict-non-JSON-object) still falls through as
    before — that is what "precedence order" legitimately means, pinned by
    ``test_payload_key_resolves_to_the_key_that_actually_coerced`` — but a
    SECOND key that also coerces (empty or not, equal or not) is now a
    parse error rather than a silently-discarded sibling. No emptiness or
    equality inspection anywhere: a sole coerced key stays valid even when
    its value is ``{}`` (case 3 — see
    ``test_sole_empty_dict_payload_still_normalizes_not_raises``).
    """
    coerced_keys: list[tuple[str, dict[str, Any]]] = []
    for key in _PAYLOAD_KEYS:
        if key in tool_input:
            # Every present key is coerced now (not just up to the first
            # match) — a malformed second key must fail closed too, rather
            # than riding along unexamined because the first key already won.
            coerced = _coerce(tool_input[key])
            if coerced is not None:
                coerced_keys.append((key, coerced))

    if len(coerced_keys) > 1:
        raise PayloadParseError(
            "tool_input carries more than one record payload ("
            + ", ".join(key for key, _ in coerced_keys)
            + ") — the confirmation card can only render one, and which one "
            "the downstream tool executes is ambiguous; supply the record "
            "under exactly one of 'data' or 'body'"
        )
    if not coerced_keys:
        raise PayloadParseError("tool_input carried no 'data' or 'body' payload")

    payload_key, record = coerced_keys[0]

    lines: list[dict[str, Any]] = []
    fields: dict[str, Any] = {}
    for line_key, value in record.items():
        if line_key in _LINE_KEYS and isinstance(value, list):
            # A non-dict entry must fail closed, not be silently dropped.
            # Dropping it would desync the confirmation card (built from
            # `.lines`) from `tool_input` (what `execute_tool_call` actually
            # sends) — the card would render fewer lines than what executes,
            # and a human approving the short card would have no way to know
            # the extra entries existed. Raising here routes through the same
            # "the write payload could not be read … NOT sent to NetSuite"
            # path every other unparseable payload already takes.
            for item in value:
                if not isinstance(item, dict):
                    raise PayloadParseError(
                        f"'{line_key}' contains a non-object entry ({item!r}) — line items must be objects"
                    )
            lines.extend(value)
        else:
            fields[line_key] = value

    record_id = tool_input.get("id") or record.get("id")
    return NormalizedPayload(
        fields=fields,
        lines=lines,
        record_id=str(record_id) if record_id is not None else None,
        record=record,
        payload_key=payload_key,
    )
