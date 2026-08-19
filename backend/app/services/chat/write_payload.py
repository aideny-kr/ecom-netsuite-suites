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
    for key in _PAYLOAD_KEYS:
        if key in tool_input:
            record = _coerce(tool_input[key])
            if record is not None:
                break

    if record is None:
        raise PayloadParseError("tool_input carried no 'data' or 'body' payload")

    lines: list[dict[str, Any]] = []
    fields: dict[str, Any] = {}
    for key, value in record.items():
        if key in _LINE_KEYS and isinstance(value, list):
            lines.extend(item for item in value if isinstance(item, dict))
        else:
            fields[key] = value

    record_id = tool_input.get("id") or record.get("id")
    return NormalizedPayload(
        fields=fields,
        lines=lines,
        record_id=str(record_id) if record_id is not None else None,
    )
