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
    return FieldSpec(
        name=raw.get("name", ""),
        label=raw.get("label") or raw.get("name", ""),
        # NetSuite metadata uses "mandatory"; accept "required" defensively.
        required=bool(raw.get("mandatory") or raw.get("required")),
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
    except Exception:
        logger.warning("record_metadata: lookup failed for %s", record_type, exc_info=True)
        return None

    if not isinstance(data, dict) or data.get("error"):
        return None

    sublists = data.get("sublists") or []
    line_fields: list[FieldSpec] = []
    for sub in sublists:
        for raw_field in (sub or {}).get("fields", []):
            line_fields.append(_parse_field(raw_field))

    meta = RecordMetadata(
        record_type=record_type,
        fields=[_parse_field(f) for f in data.get("fields", [])],
        line_fields=line_fields,
    )
    _cache[key] = (time.monotonic(), meta)
    return meta
