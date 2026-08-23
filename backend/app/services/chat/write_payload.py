"""Canonical parsing of MCP write tool_input into header fields + lines.

Every NetSuite write payload enters the system through exactly one function so
a new MCP tool schema cannot silently produce an empty confirmation card. The
Oracle NetSuite MCP sends ``data`` as a JSON *string*; older/other schemas send
``body`` as a dict. Both — and dict-valued ``data`` — normalize here.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

# Keys that hold the record payload, in precedence order.
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
    """Return the canonical payload, or raise :class:`PayloadParseError`."""
    record: dict[str, Any] | None = None
    payload_key: str | None = None
    for key in _PAYLOAD_KEYS:
        if key in tool_input:
            coerced = _coerce(tool_input[key])
            if coerced is not None:
                record, payload_key = coerced, key
                break

    if record is None or payload_key is None:
        raise PayloadParseError("tool_input carried no 'data' or 'body' payload")

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
