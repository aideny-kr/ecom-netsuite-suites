"""Fetch and cache NetSuite record-type metadata for write validation.

Returning ``None`` means "could not determine requirements" and is NOT the same
as "no required fields" — callers must render the card as ``unvalidated``
rather than assuming the payload is complete.
"""

from __future__ import annotations

import contextvars
import copy
import hashlib
import json
import logging
import time
from typing import Any

from pydantic import BaseModel

from app.services.chat.tools import execute_tool_call

logger = logging.getLogger(__name__)

_TTL_SECONDS = 3600
_cache: dict[tuple[str, ...], tuple[float, "RecordMetadata"]] = {}
# The RAW ns_getRecordTypeMetadata response, for model-issued calls that never reach
# get_record_metadata. Same key dimensions (tenant, actor, connector, credential
# revision) plus the canonical tool input; same TTL and bound. Staging showed the
# bypassing calls at 16.5 s p50 in 42 turns.
_raw_cache: dict[tuple[str, ...], tuple[float, dict]] = {}
# Set while get_record_metadata fetches. Its own cache is stamped at fetch time, so
# that fetch must be live (a raw hit would restamp an hour-old response as new) and
# must not seed the model-path cache either.
_validator_fetch: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "record_metadata_validator_fetch", default=False
)


def _connector_revision(connector) -> str | None:
    """Credential/config changes invalidate schema, including within a session.

    Only a connector loaded through the tenant-scoped service may be supplied.
    The fingerprint is never exposed as model context or written to logs.
    Authorization still runs at the eventual mutation boundary.
    """
    if not connector or connector.status != "active" or not connector.is_enabled:
        return None
    return hashlib.sha256(
        json.dumps(
            {
                "url": connector.server_url,
                "credentials": connector.encrypted_credentials,
                "auth_type": connector.auth_type,
                "metadata": connector.metadata_json,
                "updated_at": connector.updated_at,
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()


def _scoped_cache_key(connector, tenant_id, actor_id, record_type):
    revision = _connector_revision(connector)
    if revision is None:
        return None
    return (str(tenant_id), str(actor_id), str(connector.id), record_type, revision)


def _bounded_put(cache: dict, key, value) -> None:
    # Bound a process-local optimization, never persist authorization here.
    now = time.monotonic()
    for expired in [k for k, (at, _) in cache.items() if now - at >= _TTL_SECONDS]:
        cache.pop(expired, None)
    if len(cache) >= 512:
        cache.pop(min(cache, key=lambda k: cache[k][0]))
    cache[key] = (now, value)


def _remember(key, metadata):
    _bounded_put(_cache, key, metadata)


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


def coerce_netsuite_bool(value: Any) -> bool:
    """Tolerantly coerce a NetSuite-serialised boolean to a real bool.

    NetSuite serialises booleans as real `True`/`False` OR as the strings
    "T"/"F" (same convention `posting_invariants.py`'s closed-period check
    handles). A bare `bool(...)` on a string is wrong — `bool("F")` is `True`
    in Python — so this never falls back to it for a string value.

    Public because the same convention governs PAYLOAD values, not just
    metadata markers: `required_field_registry` reads `isperson` off a write
    payload to decide whether a customer needs `companyname` or `lastname`,
    and a second copy of this coercion is exactly the kind of twin that has
    drifted here before.
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
    # True only when a recognised required-marker key (see
    # `_REQUIRED_MARKER_KEYS`) was actually observed on the legacy
    # `{"fields": [...]}` shape. The live `ns_getRecordTypeMetadata` response
    # (`{"metadata": {"properties": {...}}}`) carries field NAMES only — no
    # `required`/`mandatory` array exists there and `nullable` is never
    # `false` on any field (controller-verified 2026-08-25) — so that shape
    # always sets this False. Defaults True so every pre-existing direct
    # construction of this model (tests + the legacy parse path before this
    # field existed) keeps validating exactly as before.
    requirements_known: bool = True
    # WHERE the requirements came from, when they are known at all. `None`
    # means they came from the response itself (the legacy shape's own
    # required-marker keys) or are unknown. `"curated_registry"` means a
    # human-reviewed entry in `required_field_registry` supplied them,
    # because the live NetSuite shape structurally cannot. Recorded so a log
    # or a future card caption can say which check actually ran rather than
    # implying NetSuite asserted it.
    requirements_source: str | None = None

    def required_field_names(self) -> list[str]:
        return [f.name for f in self.fields if f.required]

    def required_line_field_names(self) -> list[str]:
        return [f.name for f in self.line_fields if f.required]

    def spec_for(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)


def _raw_key(connector, tenant_id, actor_id, tool_input: dict) -> tuple[str, ...] | None:
    """(tenant, actor, connector, credential revision, canonical tool input)."""
    if actor_id is None:  # never let two actors share an entry by collapsing the key
        return None
    revision = _connector_revision(connector)
    if revision is None:
        return None
    canonical_input = json.dumps(tool_input, sort_keys=True, default=str)
    return ("raw", str(tenant_id), str(actor_id), str(connector.id), revision, canonical_input)


def cached_raw_metadata(connector, tenant_id, actor_id, tool_input: dict) -> tuple[dict, int] | None:
    """A COPY of a fresh raw metadata response for this exact scope and its age in
    seconds, or None. Never for the write validator's own fetch."""
    if _validator_fetch.get():
        return None
    key = _raw_key(connector, tenant_id, actor_id, tool_input)
    hit = _raw_cache.get(key) if key else None
    if hit:
        age = time.monotonic() - hit[0]
        if age < _TTL_SECONDS:
            return copy.deepcopy(hit[1]), int(age)
    return None


def remember_raw_metadata(connector, tenant_id, actor_id, tool_input: dict, result: dict) -> None:
    """Store a response only when the write validator's own parser accepts it as
    metadata and it does not declare itself failed. An allow-list: a failure envelope
    of any shape is fetched again next time, never served for an hour."""
    if _validator_fetch.get() or not isinstance(result, dict):
        return
    if "error" in result or result.get("isError") is True or result.get("success") is False:
        return
    try:
        usable = _parse_metadata(result, str(tool_input.get("recordType") or "")) is not None
    except Exception:
        usable = False
    key = _raw_key(connector, tenant_id, actor_id, tool_input) if usable else None
    if key is not None:
        _bounded_put(_raw_cache, key, copy.deepcopy(result))


def clear_metadata_cache() -> None:
    _cache.clear()
    _raw_cache.clear()


async def prefetch_scoped_invoice_metadata(db, tenant_id, actor_id, proposal, correlation_id):
    """Read the same native schema directly for an evidence-bound invoice update.

    The NetSuite MCP metadata tool can hit its upstream response-time ceiling
    on large invoice schemas. Use the explicitly selected REST connection;
    never guess another account or treat a timeout as a permissive schema.
    Normal curated requirements and write validation still run afterward.
    """
    from urllib.parse import urlsplit
    from uuid import UUID

    from app.services.audit_service import log_event
    from app.services.mcp_connector_service import get_mcp_connector
    from app.services.transaction_ops.netsuite_reader import authenticated_reader

    p = proposal or {}
    from app.services.transaction_ops.treatments import REGISTRY, is_mcp

    treatment = REGISTRY.get(p.get("kind"))
    if (
        p.get("tenant_id") != str(tenant_id)
        or treatment is None
        or treatment.record_type != p.get("record_type")
        or not (treatment.prefetch_metadata or is_mcp(p))
    ):
        raise ValueError("Native accounting record metadata requires the current scoped accounting proposal.")
    connector = await get_mcp_connector(db, UUID(p["connector_id"]), tenant_id)
    account = p["scope"]["netsuite_account_id"]
    if (
        not connector
        or connector.status != "active"
        or not connector.is_enabled
        or urlsplit(connector.server_url).hostname != f"{account}.suitetalk.api.netsuite.com"
    ):
        raise ValueError("The accounting record metadata connector/account binding changed.")
    record_type = p["record_type"]
    native_type = {"salesorder": "salesOrder", "creditmemo": "creditMemo", "invoice": "invoice"}[record_type]
    key = _scoped_cache_key(connector, tenant_id, actor_id, record_type)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _TTL_SECONDS:
        return
    async with authenticated_reader(db, tenant_id, p["connection_id"], account, max_api_calls=1) as reader:
        raw = await reader.request("GET", f"/record/v1/metadata-catalog/{native_type}")
    metadata = _parse_properties_shape({"metadata": raw}, record_type)
    if metadata is None or not all(metadata.spec_for(k) for k in p["proposed_fields"]):
        raise ValueError("The connected account did not provide usable accounting discount metadata.")
    await log_event(
        db,
        tenant_id,
        category="transaction_ops",
        action="accounting_correction.metadata.read",
        actor_id=actor_id,
        resource_type="transaction_case",
        resource_id=p["case_id"],
        correlation_id=correlation_id,
        payload={
            "account_id": account,
            "connection_id": p["connection_id"],
            "connector_id": p["connector_id"],
            "record_type": record_type,
            "source": "native_rest_schema",
            "requirements_known": metadata.requirements_known,
            "financial_writes": 0,
        },
    )
    _remember(key, metadata)


def _parse_properties_shape(data: dict[str, Any], record_type: str) -> "RecordMetadata | None":
    """Parse the live `ns_getRecordTypeMetadata` response shape:
    ``{"success": true, "metadata": {"type": "object", "properties": {name:
    {"title", "type", ...}}}, "message": ...}``.

    Controller-verified 2026-08-25: no key across any field's schema carries
    required/mandatory information, and `nullable` is never `false` — this
    shape can only ever yield field NAMES, never requirements. Callers must
    treat the result as ``requirements_known=False``.

    Returns None if *data* doesn't match this shape at all (so the caller can
    fall back to its existing "unknown shape" handling), or if it matches the
    shape but a property entry is malformed (present-but-wrong-type is
    "unknown", never "empty" — same rule the legacy-shape parser already
    follows for its own malformed cases).
    """
    metadata_obj = data.get("metadata")
    if not isinstance(metadata_obj, dict):
        return None
    properties = metadata_obj.get("properties")
    if not isinstance(properties, dict):
        return None

    fields: list[FieldSpec] = []
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            return None
        fields.append(
            FieldSpec(
                name=name,
                label=spec.get("title") or name,
                required=False,
                type=spec.get("type", "text"),
            )
        )

    return RecordMetadata(record_type=record_type, fields=fields, line_fields=[], requirements_known=False)


def _parse_field(raw: dict[str, Any]) -> FieldSpec:
    marker = _required_marker_value(raw)
    required = False if marker is _NO_MARKER else coerce_netsuite_bool(marker)
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
    from app.services.mcp_connector_service import get_mcp_connector

    parsed = parse_external_tool_name(mutation_tool_name)
    if not parsed:
        return None
    connector_id = parsed[0]

    connector = await get_mcp_connector(db, connector_id, tenant_id)
    key = _scoped_cache_key(connector, tenant_id, actor_id, record_type)
    if key is None:
        return None
    hit = _cache.get(key)
    if hit and (time.monotonic() - hit[0]) < _TTL_SECONDS:
        return hit[1]

    tool = _make_ext_tool_name(connector_id, "ns_getRecordTypeMetadata")
    token = _validator_fetch.set(True)
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
        meta = _parse_metadata(json.loads(raw), record_type)
    except Exception:
        logger.warning("record_metadata: lookup failed for %s", record_type, exc_info=True)
        return None
    finally:
        _validator_fetch.reset(token)

    # Cache only on the success path — a failed lookup must not be cached.
    if meta is not None:
        _remember(key, meta)
    return meta


def _parse_metadata(data: Any, record_type: str) -> RecordMetadata | None:
    """Parse a ``ns_getRecordTypeMetadata`` response in either known shape: the legacy
    ``fields``/``sublists`` one, or the live ``metadata.properties`` one. ``None``
    means unknown, never "empty"."""
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
        # Not the legacy shape at all — try the live properties shape
        # before giving up. A response matching neither still returns
        # None (unknown, never "empty").
        return _parse_properties_shape(data, record_type)
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

    return RecordMetadata(
        record_type=record_type,
        fields=fields,
        line_fields=line_fields,
        requirements_known=any_required_marker,
    )
