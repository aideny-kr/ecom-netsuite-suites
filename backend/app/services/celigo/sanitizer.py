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

`exclude` projection on the wire (the client's job, Task 3) is the first line
of defence: never ask Celigo for `mockResponse`/`mockOutput`/`sampleData`/
`rawData`/`_headers` in the first place. This module is the second line, for
when projection is bypassed, incomplete, or a future Celigo field turns out
to carry the same kind of capture under a name nobody has seen yet.

Allowlist, never denylist: a denylist of "known dangerous" field names only
stops fields someone already thought to list. An allowlist stops everything
by default and requires each field to be named IN before it survives, so an
unfamiliar field -- captured payload or not -- is dropped the same way.
"""

from __future__ import annotations

# A schema maps a Celigo field name to either:
#   None  -- a leaf: the raw value is copied verbatim, whatever its type.
#   dict  -- a nested schema: the raw value is expected to be a dict, and
#            THAT dict is filtered by this nested schema (recursively, via
#            _apply_schema), never passed through whole. A raw value that
#            isn't a dict where a nested schema is expected can't be safely
#            filtered, so it's dropped -- the same fail-closed posture as an
#            unrecognized resource_kind or an unlisted key.
Schema = dict[str, "Schema | None"]

# `aiDescription` appears on flows, exports, and imports alike (spec §3:
# "aiDescription{summary,detailed} exists on flows and imports/exports").
_AI_DESCRIPTION: Schema = {
    "summary": None,
    "detailed": None,
    "generatedOn": None,
}

# NetSuite Distributed Adaptor config on import/export steps -- named
# explicitly in spec §4.6 as Plan C's provenance input (`netsuite_da.recordType`
# + `operation`). Kept narrow on purpose: only the two fields Plan C is known
# to consume, not the adaptor's full (unverified) shape.
_NETSUITE_DA: Schema = {
    "recordType": None,
    "operation": None,
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
    "filter": None,
    "aiDescription": _AI_DESCRIPTION,
}

_IMPORT: Schema = {
    "_id": None,
    "name": None,
    "adaptorType": None,
    "_connectionId": None,
    "_sourceId": None,
    "sandbox": None,
    "mapping": None,
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
    with THEIR OWN sub-schema rather than passing them through whole."""
    out: dict = {}
    for key, sub_schema in schema.items():
        if key not in raw:
            continue
        value = raw[key]
        if sub_schema is None:
            out[key] = value
        elif isinstance(value, dict):
            out[key] = _apply_schema(value, sub_schema)
        # else: a nested schema was expected but the value isn't a dict --
        # drop it rather than guess.
    return out
