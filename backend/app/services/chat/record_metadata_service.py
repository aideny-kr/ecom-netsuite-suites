"""Fetch and cache NetSuite record-type metadata for write validation.

Returning ``None`` means "could not determine requirements" and is NOT the same
as "no required fields" — callers must render the card as ``unvalidated``
rather than assuming the payload is complete.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from pydantic import BaseModel

from app.services.chat.tools import execute_tool_call

logger = logging.getLogger(__name__)

_TTL_SECONDS = 3600
_cache: dict[tuple[str, str], tuple[float, "RecordMetadata"]] = {}

# NetSuite has been observed to serialise the required-marker under several
# names depending on endpoint/version (`ismandatory` on discovered account
# metadata per prompt_template_service.py:87, `mandatory` in our own fixture
# shape). The live `ns_getRecordTypeMetadata` shape has never been captured
# (MCP token expired — see backend/tests/fixtures/netsuite_metadata/README.md),
# so every plausible key is accepted rather than betting on one. Order is the
# tie-break when a payload somehow carries more than one; first present wins.
_REQUIRED_MARKER_KEYS: tuple[str, ...] = (
    "mandatory",
    "ismandatory",
    "required",
    "isrequired",
    "isMandatory",
    "isRequired",
)

_TRUTHY_STRINGS = {"t", "true", "yes", "1"}

# Sentinel distinguishing "no recognised marker key present" from "marker key
# present with a falsy value" — the two must not be treated the same, or a
# field with e.g. {"mandatory": False} would fall through to a later key.
_NO_MARKER = object()


def _coerce_required_flag(value: Any) -> bool:
    """Tolerantly coerce NetSuite's required-marker to a real bool.

    NetSuite serialises booleans as real `True`/`False` OR as the strings
    "T"/"F" (same convention `posting_invariants.py`'s closed-period check
    handles). A bare `bool(...)` on a string is wrong — `bool("F")` is `True`
    in Python — so this never falls back to it for a string value.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY_STRINGS
    return bool(value)


def _required_marker_value(raw: dict[str, Any]) -> Any:
    """Return the raw value under the first recognised marker key present in
    *raw*, or `_NO_MARKER` if none of them appear at all."""
    for key in _REQUIRED_MARKER_KEYS:
        if key in raw:
            return raw[key]
    return _NO_MARKER


class FieldSpec(BaseModel):
    name: str
    label: str
    required: bool = False
    type: str = "text"
    options: list[dict[str, Any]] | None = None


class RecordMetadata(BaseModel):
    record_type: str
    fields: list[FieldSpec] = []
    line_fields: list[FieldSpec] = []

    def required_field_names(self) -> list[str]:
        return [f.name for f in self.fields if f.required]

    def required_line_field_names(self) -> list[str]:
        return [f.name for f in self.line_fields if f.required]

    def spec_for(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)


def clear_metadata_cache() -> None:
    _cache.clear()


def _parse_field(raw: dict[str, Any]) -> FieldSpec:
    marker = _required_marker_value(raw)
    required = False if marker is _NO_MARKER else _coerce_required_flag(marker)
    return FieldSpec(
        name=raw.get("name", ""),
        label=raw.get("label") or raw.get("name", ""),
        required=required,
        type=raw.get("type", "text"),
        options=raw.get("options"),
    )


async def get_record_metadata(
    *,
    record_type: str,
    mutation_tool_name: str,
    tenant_id: Any,
    actor_id: Any,
    correlation_id: str,
    db: Any,
    session_id: str,
) -> RecordMetadata | None:
    """Return metadata for *record_type*, or ``None`` if it cannot be fetched."""
    from app.services.chat.tools import _make_ext_tool_name, parse_external_tool_name

    parsed = parse_external_tool_name(mutation_tool_name)
    if not parsed:
        return None
    connector_id = parsed[0]

    key = (str(connector_id), record_type)
    hit = _cache.get(key)
    if hit and (time.monotonic() - hit[0]) < _TTL_SECONDS:
        return hit[1]

    tool = _make_ext_tool_name(connector_id, "ns_getRecordTypeMetadata")
    try:
        raw = await execute_tool_call(
            tool_name=tool,
            tool_input={"recordType": record_type},
            tenant_id=tenant_id,
            actor_id=actor_id,
            correlation_id=correlation_id,
            db=db,
            session_id=session_id,
        )
        data = json.loads(raw)

        if not isinstance(data, dict) or data.get("error"):
            return None

        raw_fields = data.get("fields")
        has_sublists_key = "sublists" in data
        raw_sublists = data.get("sublists")

        # Present-but-wrong-type is "unknown", not "empty" — a malformed shape
        # must not be reported as "this record type has no required fields".
        # A genuinely *absent* "sublists" key is a valid "no line items" shape
        # and must not be conflated with a present-but-null/wrong-type one —
        # `.get()` returns None for both, so the presence check is required to
        # tell them apart.
        if not isinstance(raw_fields, list):
            return None
        if has_sublists_key and not isinstance(raw_sublists, list):
            return None

        line_fields: list[FieldSpec] = []
        for sub in raw_sublists or []:
            if not isinstance(sub, dict):
                return None
            sub_fields = sub.get("fields", [])
            if not isinstance(sub_fields, list):
                return None
            for raw_field in sub_fields:
                if not isinstance(raw_field, dict):
                    return None
                line_fields.append(_parse_field(raw_field))

        fields: list[FieldSpec] = []
        any_required_marker = False
        for raw_field in raw_fields:
            if not isinstance(raw_field, dict):
                return None
            if _required_marker_value(raw_field) is not _NO_MARKER:
                any_required_marker = True
            fields.append(_parse_field(raw_field))

        # A shape mismatch (none of the recognised marker keys present on any
        # field) degrades silently into "nothing is required" — which reads
        # exactly like a legitimately permissive record type. Make it LOUD
        # rather than fatal: a genuinely permissive record type is possible,
        # so this must not block the write, only flag the shape for a human.
        if raw_fields and not any_required_marker:
            first_field = raw_fields[0]
            observed_keys = sorted(first_field.keys()) if isinstance(first_field, dict) else []
            logger.warning(
                "record_metadata: no recognised required-marker key (%s) found on any field "
                "for record type %r; keys observed on first field: %s",
                ", ".join(_REQUIRED_MARKER_KEYS),
                record_type,
                observed_keys,
            )

        meta = RecordMetadata(record_type=record_type, fields=fields, line_fields=line_fields)
    except Exception:
        logger.warning("record_metadata: lookup failed for %s", record_type, exc_info=True)
        return None

    # Cache only on the success path — a failed lookup must not be cached.
    _cache[key] = (time.monotonic(), meta)
    return meta
