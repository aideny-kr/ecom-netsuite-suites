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


_ASK_USER_KEY = "ask_user"


def _strip_ask_user(value: Any) -> Any:
    """Recursively drop every ``ask_user`` key. Returns a NEW structure."""
    if isinstance(value, dict):
        return {k: _strip_ask_user(v) for k, v in value.items() if k != _ASK_USER_KEY}
    if isinstance(value, list):
        return [_strip_ask_user(v) for v in value]
    return value


def sanitize_tool_input(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *tool_input* with every ``ask_user`` key removed.

    THIS is what must be signed and executed — not merely what the card
    displays. Stripping inside ``normalize_write_payload`` cleaned only the
    display struct: ``build_confirmation_payload`` signs and stores the RAW
    ``tool_input``, and a plain approve (no slot edited) hands that verbatim to
    ``execute_tool_call``. So the hint still reached NetSuite, which rejects an
    unknown field — the 400 an operator actually hit on 2026-08-27, reported as
    fixed when only the display had been.

    Recursive, because the first attempt also missed a level: a key nested
    inside a LINE ITEM survived, since lines are extended wholesale with no
    per-item filtering.

    Two properties the callers depend on:

    * The input is NEVER mutated. The caller still holds the original, and
      rewriting it underneath them is how a signed envelope and its payload
      drift apart.
    * Encoding is preserved. ``data`` arriving as a JSON string leaves as a
      JSON string; the external MCP is shape-sensitive. A payload with nothing
      to strip round-trips byte-identical, so tokens minted over it stay
      stable.

    Never raises: an unparseable payload is returned untouched and handled
    downstream by ``PayloadParseError``, which owns that case.
    """
    if not isinstance(tool_input, dict):
        return tool_input

    out: dict[str, Any] = {}
    changed = False
    for key, value in tool_input.items():
        if key == _ASK_USER_KEY:
            # base_agent pops this from the top level first, but a second
            # entry point must not depend on that having happened.
            changed = True
            continue
        if key in _PAYLOAD_KEYS:
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except (ValueError, TypeError):
                    out[key] = value
                    continue
                cleaned = _strip_ask_user(parsed)
                if cleaned != parsed:
                    out[key] = json.dumps(cleaned)
                    changed = True
                else:
                    out[key] = value
                continue
            if isinstance(value, (dict, list)):
                cleaned = _strip_ask_user(value)
                if cleaned != value:
                    changed = True
                out[key] = cleaned
                continue
        out[key] = value
    return out if changed else tool_input


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
        if line_key == _ASK_USER_KEY:
            # Out-of-band hint, never a record field. `base_agent` pops it
            # from the TOP level of tool_input, but the model has been
            # observed nesting it inside the data payload instead (staging,
            # 2026-08-27) — where the top-level pop cannot see it. Left in, it
            # renders as a bogus field row on the confirmation card AND gets
            # posted to NetSuite as a record field on approve, which NetSuite
            # rejects. Dropped at the one place every payload is parsed, so no
            # nesting level survives.
            continue
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
