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

FIX ROUND 2 (2026-08-27): a follow-up probe found `include` fails the same
way as `exclude` -- an export fetched with a positive allowlist that never
named a payload field still came back with `mockOutput` and `rawData`
unrequested. Neither projection direction works; this sanitizer really is
the only control. That also surfaced an opposite-direction risk: a
`_scriptId` is an object id, not captured payload data, and sanitizing must
never destroy one -- Task 2's script-graph walker finds every `_scriptId`,
and if it ever runs on sanitized (stored) data rather than the raw response,
a dropped attachment site is a script that silently does not exist as far as
the flow map is concerned. `filter.script`/`transform.script` and `hooks.*`
(an open-ended, never-enumerated key set) are allowlisted for exactly this
reason -- see `_SCRIPT_REF` below.

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
#
# ONE exception: a schema containing the literal key `_WILDCARD` applies its
# sub-schema to EVERY key present in the raw dict, by position rather than by
# name (see `_apply_schema`). This is how `hooks.*` allowlists an
# open-ended, never-enumerated set of hook names without hardcoding a
# taxonomy -- the allowlisting still happens, just one level down.
Schema = dict[str, "Schema | None"]

_WILDCARD = "*"  # not a real Celigo field name; sentinel for `hooks.*`

# `aiDescription` appears on flows, exports, and imports alike (spec §3:
# "aiDescription{summary,detailed} exists on flows and imports/exports").
_AI_DESCRIPTION: Schema = {
    "summary": None,
    "detailed": None,
    "generatedOn": None,
}

# `_scriptId` marks a script attachment site (plan's Verified Facts: "Scripts
# attach at transform.script, hooks.*, filter, router branches -- recursive
# walk, not an enumerated hook list"). A `_scriptId` is an object id, not
# captured payload data -- there is no reason to drop one, and doing so
# silently empties the flow map (see FIX ROUND 2 in the module docstring).
_SCRIPT_REF: Schema = {
    "_scriptId": None,
    "function": None,
}

# `filter` shape CONFIRMED live on import objects (observed-shapes.md, fix
# round 1): {type, expression: {rules, version}, rules, version}. `type` can
# be "expression" (this shape, no script -- CONFIRMED live, import section)
# or "script", in which case a nested `script: {_scriptId, function}` field
# carries the attachment (fix round 2) and MUST survive -- do not key on
# `type` or on the presence of the `filter` key itself, both forms are
# allowlisted here.
_FILTER_EXPRESSION: Schema = {
    "rules": None,
    "version": None,
}
_FILTER: Schema = {
    "type": None,
    "expression": _FILTER_EXPRESSION,
    "rules": None,
    "version": None,
    "script": _SCRIPT_REF,
}

# `transform` shape CONFIRMED live on export objects (observed-shapes.md, fix
# round 2) -- the export-side counterpart to import's `filter`, same two
# forms: `type: "expression"` (CONFIRMED live, no script) or `type: "script"`
# with a nested `script: {_scriptId, function}` -- "THE most-used [script]
# site in the live account" per the plan's Verified Facts.
_TRANSFORM_EXPRESSION: Schema = {
    "rules": None,
    "version": None,
}
_TRANSFORM: Schema = {
    "type": None,
    "expression": _TRANSFORM_EXPRESSION,
    "rules": None,
    "version": None,
    "script": _SCRIPT_REF,
}

# `hooks.*` -- e.g. `hooks: {preSavePage: {_scriptId, function}}` (plan's
# Verified Facts). The hook NAME is open-ended and must not be enumerated: a
# hook type nobody has seen yet still keeps its script reference. See
# `_apply_schema`'s `_WILDCARD` handling.
_HOOKS: Schema = {
    _WILDCARD: _SCRIPT_REF,
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

# NOTE (fix round 2): observed-shapes.md's live export probe lists `transform`
# among the object's observed top-level keys but not `filter` -- unlike
# `aiDescription` (explicitly confirmed uneven coverage), that's not
# affirmative proof `filter` never appears on export, just that this one
# probed object didn't have it configured. `filter` is kept rather than
# removed on absence-of-evidence (it's a safe, recursively-filtered schema,
# not a leaf, so keeping it costs nothing); `transform` is ADDED as the newly
# confirmed, more heavily used field. Flagged for whoever probes export data
# next to confirm/prune.
_EXPORT: Schema = {
    "_id": None,
    "name": None,
    "adaptorType": None,
    "_connectionId": None,
    "_sourceId": None,
    "sandbox": None,
    "filter": _FILTER,
    "transform": _TRANSFORM,
    "hooks": _HOOKS,
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
    "hooks": _HOOKS,
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
    if _WILDCARD in schema:
        # hooks.*: every key in *raw* survives BY POSITION rather than by
        # name -- an un-enumerated hook type still keeps its script
        # reference. This is NOT a pass-through: the allowlisting still
        # happens, just one level down, via the wildcard's own fixed
        # sub-schema -- a captured payload nested inside a hook entry is
        # still stripped by the recursive call below. Non-dict values can't
        # be safely filtered and are dropped, same as everywhere else.
        sub_schema = schema[_WILDCARD]
        return {key: _apply_schema(value, sub_schema) for key, value in raw.items() if isinstance(value, dict)}

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
