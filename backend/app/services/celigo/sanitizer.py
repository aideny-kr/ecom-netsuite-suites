"""Allowlist sanitizer for captured Celigo payloads.

Plan B scope: keep live production data Celigo embeds in its own objects out
of our database. Probed live on 2026-08-25 against the real Solidus +
NetSuite integration (see
docs/superpowers/specs/2026-08-25-celigo-flow-map-design.md §3/§4.3), an
import object's `mockResponse` was found to contain a live `set-cookie`
session header for `.frame.work`, a full customer record, and product data.
Celigo's "test connection"/"preview mapping" feature stores that capture on
the resource's own config, and it can recur at any nesting level -- not only
the top level.

FIX ROUND 1 (2026-08-27): re-probed live, `exclude` projection on the wire
does NOT reliably strip payload fields -- `GET /v1/imports/{id}` with
`exclude=...,mockResponse,...` still returned `mockResponse`. THIS SANITIZER
IS THE ONLY EFFECTIVE DEFENCE, not a backstop behind projection. That raises
the bar on every allowlisted field: any field whose value CAN be a nested
object or a list of them (`filter`, `netsuite_da.mapping`) must be
recursively filtered by its own schema, never blob-copied as a leaf -- a
leaf copy is exactly the shape of hole a captured payload slips through.

Allowlist, never denylist: a denylist of "known dangerous" field names only
stops fields someone already thought to list. An allowlist stops everything
by default and requires each field to be named IN before it survives, so an
unfamiliar field -- captured payload or not -- is dropped the same way.
"""

from __future__ import annotations

# A schema maps a Celigo field name to either:
#   None  -- a leaf: the raw value is copied verbatim, whatever its type.
#   dict  -- a nested schema: the raw value is expected to be a dict (filtered
#            recursively by this nested schema) OR a list of dicts (each item
#            filtered the same way, non-dict items dropped) -- never passed
#            through whole either way. A raw value that is neither a dict nor
#            a list of dicts where a nested schema is expected can't be
#            safely filtered, so it's dropped -- the same fail-closed posture
#            as an unrecognized resource_kind or an unlisted key.
Schema = dict[str, "Schema | None"]

# `aiDescription` appears on flows, exports, and imports alike (spec §3:
# "aiDescription{summary,detailed} exists on flows and imports/exports").
_AI_DESCRIPTION: Schema = {
    "summary": None,
    "detailed": None,
    "generatedOn": None,
}

# `filter` shape CONFIRMED live on both export and import objects
# (observed-shapes.md, fix round 1): {type, expression: {rules, version},
# rules, version}. `type` can be "expression" (this shape) or "script" (not
# yet observed live -- a script-type filter's own `script: {_scriptId, ...}`
# field is NOT in this schema and will be dropped until that shape is
# verified; flagged in the fix report rather than guessed at here).
_FILTER_EXPRESSION: Schema = {
    "rules": None,
    "version": None,
}
_FILTER: Schema = {
    "type": None,
    "expression": _FILTER_EXPRESSION,
    "rules": None,
    "version": None,
}

# `netsuite_da.mapping` shape CONFIRMED live (observed-shapes.md, fix round
# 1): {fields: [{extract, generate, internalId}], lists: []}. `fields` is a
# LIST of dicts -- each one filtered by its own schema below, not
# blob-copied. `lists`'s element shape was observed empty and is genuinely
# unverified, so it is deliberately left OUT of this schema (dropped) rather
# than guessed at -- the same fail-closed posture as everything else here.
_MAPPING_FIELD: Schema = {
    "extract": None,
    "generate": None,
    "internalId": None,
}
_MAPPING: Schema = {
    "fields": _MAPPING_FIELD,
}

# NetSuite Distributed Adaptor config on import/export steps -- named
# explicitly in spec §4.6 as Plan C's provenance input (`netsuite_da.recordType`
# + `operation`). Kept narrow on purpose: only the fields Plan C is known to
# consume or that are verified above, not the adaptor's full shape --
# `restletVersion`/`internalIdLookup`/`lookups` were observed live too but are
# out of scope for this fix (see fix report).
_NETSUITE_DA: Schema = {
    "recordType": None,
    "operation": None,
    "mapping": _MAPPING,
}

_INTEGRATION: Schema = {
    "_id": None,
    "name": None,
    "sandbox": None,
    "mode": None,
    "description": None,
    "lastModified": None,
}

_FLOW: Schema = {
    "_id": None,
    "name": None,
    "_integrationId": None,
    "disabled": None,
    "schedule": None,
    "timezone": None,
    "lastExecutedAt": None,
    "_sourceId": None,
    "numOpenError": None,
    "lastErrorAt": None,
    "aiDescription": _AI_DESCRIPTION,
}

_EXPORT: Schema = {
    "_id": None,
    "name": None,
    "adaptorType": None,
    "_connectionId": None,
    "_sourceId": None,
    "sandbox": None,
    "filter": _FILTER,
    "aiDescription": _AI_DESCRIPTION,
}

# NOTE (fix round 1): the real `GET /v1/imports/{id}` response has no
# top-level `mapping` key -- that was this module's own inferred-not-verified
# guess, and observed-shapes.md proves it wrong. The real mapping config
# lives at `netsuite_da.mapping` (now schema'd above). `filter` IS confirmed
# on import objects and was previously missing here entirely.
_IMPORT: Schema = {
    "_id": None,
    "name": None,
    "adaptorType": None,
    "_connectionId": None,
    "_sourceId": None,
    "sandbox": None,
    "filter": _FILTER,
    "netsuite_da": _NETSUITE_DA,
    "aiDescription": _AI_DESCRIPTION,
}

_SCRIPT: Schema = {
    "_id": None,
    "name": None,
    "content": None,
    "_sourceId": None,
    "sandbox": None,
}

# Field names observed live (spec §3): "Error objects carry traceKey (source-
# system record id), errorId, retryDataKey, source, code, message, _flowJobId,
# occurredAt, purgeAt". `retriable` is stored too (spec §4, plan: "store it,
# never branch on it as if it meant retry will work").
#
# `message` is deliberately NOT stripped or scrubbed here even though it
# demonstrably contains PII (spec §4.3: GDPR-scrubbed customer emails, order
# refs) -- it is the diagnostic payload the sanitizer exists to preserve
# verbatim. PII-safe grouping is the signature fingerprint's job (a later
# task), not this allowlist's.
_ERROR: Schema = {
    "traceKey": None,
    "errorId": None,
    "retryDataKey": None,
    "source": None,
    "code": None,
    "message": None,
    "occurredAt": None,
    "purgeAt": None,
    "_flowJobId": None,
    "retriable": None,
}

_ALLOWLISTS: dict[str, Schema] = {
    "integration": _INTEGRATION,
    "flow": _FLOW,
    "export": _EXPORT,
    "import": _IMPORT,
    "script": _SCRIPT,
    "error": _ERROR,
}


def sanitize(resource_kind: str, raw: dict) -> dict:
    """Return a NEW dict containing only the fields allowlisted for *resource_kind*.

    Never mutates *raw*. An unrecognized *resource_kind* has no allowlist, so
    nothing about it is provably safe -- this fails CLOSED and returns an
    empty dict rather than passing the object through untouched.
    """
    schema = _ALLOWLISTS.get(resource_kind)
    if schema is None:
        return {}
    return _apply_schema(raw, schema)


def _apply_schema(raw: dict, schema: Schema) -> dict:
    """Copy only the keys present in *schema*, recursing into nested dicts
    (or lists of them) with THEIR OWN sub-schema rather than passing them
    through whole."""
    out: dict = {}
    for key, sub_schema in schema.items():
        if key not in raw:
            continue
        value = raw[key]
        if sub_schema is None:
            # A leaf field. Dict/list values are still copied ONE level
            # shallow rather than aliased, so a caller mutating the returned
            # structure can't reach back into *raw* through it -- purity
            # holds for the caller's copy too, not just for sanitize()'s own
            # read of *raw*.
            out[key] = dict(value) if isinstance(value, dict) else (list(value) if isinstance(value, list) else value)
        elif isinstance(value, dict):
            out[key] = _apply_schema(value, sub_schema)
        elif isinstance(value, list):
            # e.g. netsuite_da.mapping.fields: a list of dicts, each filtered
            # by the SAME nested schema -- a leaf copy here would blob-copy
            # every item whole, the same hole a captured payload slips
            # through if nested inside one of them.
            out[key] = [_apply_schema(item, sub_schema) for item in value if isinstance(item, dict)]
        # else: a nested schema was expected but the value is neither a dict
        # nor a list of dicts -- drop it rather than guess.
    return out
