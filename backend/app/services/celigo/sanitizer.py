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

FIX ROUND 3 (2026-08-27): the same silent-failure class as round 2, wider
blast radius -- `_FLOW` had no schema for `pageGenerators`/`pageProcessors`/
`routers` at all, so a real flow sanitized down to `{_id, name}`. That IS the
flow map: Task 5 builds `celigo_flow_steps` from exactly these ids. Worse,
per a live probe across 60 real flows (observed-shapes.md "routers"
section), every multi-subsidiary sales-order flow -- the ones the recon
chain depends on -- puts its steps inside `routers[].branches[].
pageProcessors`, not the top-level arrays, so the gap hit hardest exactly
where it mattered most. Step/branch ids and routing config are ids and
config, not captured payload data; there is no case where dropping them is
correct. See `_PAGE_GENERATOR`/`_PAGE_PROCESSOR`/`_ROUTER` below.

FIX ROUND 4 (2026-08-27): a seam between this module and Task 2's
`walk_script_refs` (app.services.celigo.graph) -- neither task's own tests
could catch it alone, only composing the two: `walk_script_refs(sanitize(x))`
found FEWER refs than `walk_script_refs(x)` for imports and for flows with an
inlined step script. `_IMPORT` had `filter`/`hooks` but never gained
`transform` when round 2 added it to `_EXPORT` -- an artifact of what got
probed, not a real API distinction, and it dropped "the regression case...
the most-used script in the live account" (plan's Verified Facts) on every
import. `_PAGE_PROCESSOR` (round 3) also had no `hooks`/`transform`/`filter`
of its own, so a script ref inlined inside a flow-embedded pageProcessor
would be lost too (not observed live, added anyway -- see `_PAGE_PROCESSOR`).
`TestSanitizerAndWalkerCompose` in the test file asserts the composition
property directly (`walk_script_refs(sanitize(kind, obj)) ==
walk_script_refs(obj)` as sets) for the three kinds it hand-writes fixtures
for.

CORRECTED BY ROUND 7 -- READ THIS BEFORE TRUSTING THE PARAGRAPH ABOVE: that
test's own docstring, and this one, originally claimed round 4 had made "this
whole class of defect unrepresentable rather than fixing two instances of
it", covering "every kind that can carry scripts". BOTH CLAIMS WERE FALSE
WHEN WRITTEN. Round 4 fixed two of five attachment sites and pinned only
those two; three siblings (`_PAGE_GENERATOR`, `_ROUTER_BRANCH`, and the
branch's `inputFilter`) went on silently dropping real refs, proven by
execution in the whole-branch review. Three hand-written fixtures can only
cover the containers whoever wrote them thought of, which is not a
structural property at all. See fix round 7 below for what replaced it.

FIX ROUND 5 (2026-08-27, FINAL ROUND): the third instance of the same
pattern -- round 2 dropped script refs, round 3 dropped flow topology, this
one silently dropped `lastModified` for `_FLOW`/`_SCRIPT` (their
`celigo_last_modified` columns, migration 094, stayed permanently NULL --
`_INTEGRATION` already had it correctly). `TestSanitizerPreservesEveryRepositoryReadField`
in the test file asserts, for every kind `app.services.celigo.repository`'s
`upsert_*` functions actually consume a sanitized dict for, that every field
those functions read TODAY survives sanitize() -- calling `extract_flow_steps`
(the real pure consumer) directly for the flow case, the same "compose with
the real downstream" pattern round 4 used for `walk_script_refs`.

STATED PLAINLY SO THIS DOCSTRING DOESN'T MAKE THE SAME MISTAKE ITSELF: that
test PINS today's known read-set by hand; it does NOT derive it from
repository.py. A field repository.py starts reading tomorrow, with no
matching assertion added to that test, still lands permanently NULL -- see
that test's own docstring for what a structural fix (an AST scan or a
repository-side assertion) would take, deliberately not implemented here.

FIX ROUND 6 (2026-08-27, whole-branch review finding 3): round 1 wrote the
rule and then left one field breaking it. `rules` stayed a schema LEAF, and
`_apply_schema`'s leaf branch copies exactly one level deep -- so a
`mockResponse` carrying a session cookie, a customer email and an order ref,
nested under `filter.expression.rules`, came through `sanitize("import", ...)`
VERBATIM. Same via `transform.expression.rules` and
`routers[].branches[].inputFilter.rules`. Not a hypothetical: rounds 1 and 2
proved neither projection direction keeps captures off the wire, so this is
the only thing standing between a live payload and our DB. `rules` cannot
take a key schema (its real values are arbitrarily-nested EXPRESSION TREES,
not fixed-key objects), so it gets a third schema kind -- `_RULE_TREE`,
filtered by value shape -- see `_RULE_DICT_KEYS` and `_filter_rule_tree`
below. With that, no allowlisted field is a blob-copied leaf any more.

FIX ROUND 7 (2026-08-27, whole-branch review finding 2): the sibling half of
round 4, plus the correction to what round 4 claimed about itself (see
above). `_PAGE_GENERATOR` never got the `hooks`/`transform`/`filter` that
`_PAGE_PROCESSOR` did; `_ROUTER_BRANCH` never got the `script` that `_ROUTER`
has, though the plan's Verified Facts name router branches as an attachment
site explicitly; and the branch's `inputFilter` was schema'd as a bare
`{rules}` unlike its `filter`/`transform` cousins. All three lost real refs.

Fixing three more instances would have been the fourth round of the same
move. Instead the site list became ONE definition, `_SCRIPT_SITES`, spliced
into every container -- so "some sites but not all" is no longer expressible
-- and the tests derive both the site list and the containers from this
table (`TestScriptSiteCoverageIsDerivedFromTheSchema`): any node declaring
one site must declare all five, and any node declaring a TOPOLOGY key
(`_TOPOLOGY_KEYS`: a step reference, a branch, or a container of them) must
carry every site. A future sibling of `_PAGE_GENERATOR` cannot model
topology without naming one of those keys, so it is covered without anyone
remembering to update a test. What that still cannot cover -- a site key
Celigo invents that nobody has seen -- is stated in that test's docstring
rather than papered over.

Allowlist, never denylist: a denylist of "known dangerous" field names only
stops fields someone already thought to list. An allowlist stops everything
by default and requires each field to be named IN before it survives, so an
unfamiliar field -- captured payload or not -- is dropped the same way.
"""

from __future__ import annotations

# A schema maps a Celigo field name to one of:
#   None        -- a leaf: the raw value is copied verbatim, whatever its type.
#   dict        -- a nested schema: the raw value is expected to be a dict
#                  (filtered recursively by this nested schema) OR a list of
#                  dicts (each item filtered the same way, non-dict items
#                  dropped) -- never passed through whole either way. A raw
#                  value that is neither a dict nor a list of dicts where a
#                  nested schema is expected can't be safely filtered, so it's
#                  dropped -- the same fail-closed posture as an unrecognized
#                  resource_kind or an unlisted key.
#   _RULE_TREE  -- an EXPRESSION TREE: arbitrarily nested lists/scalars with
#                  small config dicts at the leaves, filtered by VALUE SHAPE
#                  rather than by a key schema (see `_filter_rule_tree`).
#
# ONE exception: a schema containing the literal key `_WILDCARD` applies its
# sub-schema to EVERY key present in the raw dict, by position rather than by
# name (see `_apply_schema`). This is how `hooks.*` allowlists an
# open-ended, never-enumerated set of hook names without hardcoding a
# taxonomy -- the allowlisting still happens, just one level down.
Schema = dict[str, "Schema | None | _RuleTreeMarker"]

_WILDCARD = "*"  # not a real Celigo field name; sentinel for `hooks.*`

# Depth ceiling for the two SHAPE-driven recursions below (`_deep_copy_leaf`
# and `_filter_rule_tree`) -- FIX ROUND 9, scoped re-review R4. Schema-driven
# recursion (`_apply_schema`) is bounded by the schemas themselves, which are
# finite and shallow; those two are not bounded by anything but Python's call
# stack. Measured on this branch before the bound existed: `json.loads`
# accepts 9997 levels of nesting (that is what `response.json()` runs), while
# sanitize() blew the stack at 996 -- a ~9000-level window where a Celigo
# response parses on the wire and then raises `RecursionError` inside this
# module. It failed closed and fast, but `RecursionError` is an accident of
# the interpreter, not a decision this module made.
#
# 100 is chosen against measured evidence, not taste: the deepest structure
# anywhere in this repo's Celigo corpus (every fixture plus the schemas
# themselves) is 10 levels, so this is 10x the deepest thing ever observed,
# and still an order of magnitude under the recursion limit -- the bound
# fires as a deliberate refusal, never as a stack overflow.
_MAX_SANITIZE_DEPTH = 100


class CeligoSanitizeDepthError(Exception):
    """A Celigo payload nested deeper than `_MAX_SANITIZE_DEPTH` inside a
    schema leaf or a `rules` expression tree.

    Fails CLOSED, exactly like every other refusal in this module: nothing is
    returned, so nothing partially-filtered can be stored. Raised instead of
    letting `RecursionError` escape so the failure names its own cause and its
    own limit rather than surfacing as an interpreter-level stack overflow
    from somewhere inside a comprehension."""


class _RuleTreeMarker:
    """Sentinel schema value for `rules` -- see `_filter_rule_tree`. A
    distinct type rather than another magic string so it can never collide
    with a real Celigo field name and so `is` identity, not equality,
    decides which branch of `_apply_schema` runs."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid only
        return "_RULE_TREE"


_RULE_TREE = _RuleTreeMarker()

# FIX ROUND 6 (2026-08-27, final-review finding 3): `rules` was the LAST
# field still declared a schema LEAF, and a leaf copy is one level shallow --
# so a `mockResponse` carrying a session cookie, a customer email and an
# order ref, nested anywhere under `filter.expression.rules`, passed
# `sanitize("import", ...)` VERBATIM. That is this module's own round-1
# invariant broken word for word ("never blob-copied as a leaf -- a leaf copy
# is exactly the shape of hole a captured payload slips through"), in the one
# place where it is the only control there is (both projection directions
# were proven useless live -- see rounds 1 and 2 above).
#
# `rules` could not simply get a key schema the way `filter` and
# `netsuite_da.mapping` did in round 1, because real `rules` values are
# EXPRESSION TREES, not fixed-key objects. Both of these are real shapes from
# observed-shapes.md's live probes:
#
#     ["notempty", ["string", ["extract", "..."]]]   -- nested lists of strings
#     [[{key, extract, generate}]]                    -- list of list of dicts
#
# There is no top-level key set to enumerate. So `rules` is filtered by VALUE
# SHAPE instead: lists recurse, scalars pass through, and DICTS keep only the
# keys named below. A captured payload is always a dict under some key
# (`mockResponse`, `mockOutput`, `rawData`, `_headers`, `sampleData`), so
# dropping unlisted dict keys at EVERY depth closes the hole while leaving
# every legitimate expression tree byte-identical.
#
# The key list is OBSERVATION-DERIVED, not guessed: `key`/`extract`/`generate`
# are the only keys seen inside a real `rules` tree. Stated plainly, since
# this cuts both ways: a legitimate rule key nobody has observed yet IS
# dropped, which loses filter/mapping detail from the flow map until someone
# probes it and adds it here. That direction is recoverable; the other one
# (widening on a guess and blob-copying a capture into the DB forever) is
# not, and this module's fail-closed posture -- `netsuite_da.mapping.lists`
# dropped in round 1 for exactly this reason -- decides it the same way.
_RULE_DICT_KEYS = frozenset({"key", "extract", "generate"})

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
    "rules": _RULE_TREE,
    "version": None,
}
_FILTER: Schema = {
    "type": None,
    "expression": _FILTER_EXPRESSION,
    "rules": _RULE_TREE,
    "version": None,
    "script": _SCRIPT_REF,
}

# `transform` shape CONFIRMED live on export objects (observed-shapes.md, fix
# round 2) -- the export-side counterpart to import's `filter`, same two
# forms: `type: "expression"` (CONFIRMED live, no script) or `type: "script"`
# with a nested `script: {_scriptId, function}` -- "THE most-used [script]
# site in the live account" per the plan's Verified Facts.
_TRANSFORM_EXPRESSION: Schema = {
    "rules": _RULE_TREE,
    "version": None,
}
_TRANSFORM: Schema = {
    "type": None,
    "expression": _TRANSFORM_EXPRESSION,
    "rules": _RULE_TREE,
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

# FIX ROUND 7 (2026-08-27, whole-branch review finding 2): EVERY key a script
# can hang off, defined ONCE, spliced into every container that models flow
# topology -- never hand-copied into one and forgotten in its sibling.
#
# That forgetting is the entire defect this replaces. Round 4 added
# `hooks`/`transform`/`filter` to `_PAGE_PROCESSOR` but not to
# `_PAGE_GENERATOR`; `_ROUTER` got `script` but `_ROUTER_BRANCH` did not,
# though the plan's Verified Facts name router branches as an attachment site
# explicitly; and `inputFilter` was schema'd as a bare `{rules}` unlike its
# `filter`/`transform` cousins. Three sites lost refs live -- and round 4's
# own docstring claimed it had made "this whole class of defect
# unrepresentable rather than fixing two instances of it". It had fixed two
# of five.
#
# With one definition spliced in, "half the sites" is no longer expressible:
# a container either splices this dict and gets all of them, or it doesn't
# and `TestScriptSiteCoverageIsDerivedFromTheSchema` fails. Adding a SIXTH
# site key here covers every container at once; adding a new container that
# models topology is caught by that test's topology-key rule.
#
# `inputFilter` reuses `_FILTER`: Celigo's naming is inconsistent (a branch's
# is `inputFilter`, an export's is `filter`) but the shapes are the same
# family, and every one observed live -- `{rules: [...]}` -- is a subset of
# `_FILTER`. Treating them alike costs nothing (both are recursively
# filtered, neither can pass a payload) and removes a distinction that only
# ever produced a gap.
_SCRIPT_SITES: Schema = {
    "script": _SCRIPT_REF,
    "hooks": _HOOKS,
    "transform": _TRANSFORM,
    "filter": _FILTER,
    "inputFilter": _FILTER,
}

# Keys that mean "this schema node models part of a flow's topology" -- a
# step reference, a branch, or a container of them. Derived from the schema
# table by `TestScriptSiteCoverageIsDerivedFromTheSchema`, which requires
# every node declaring one of these to splice `_SCRIPT_SITES` in full. This
# is what makes a FUTURE sibling of `_PAGE_GENERATOR`/`_ROUTER_BRANCH`
# covered without anyone remembering to update a test: it cannot model
# topology without naming one of these keys.
_TOPOLOGY_KEYS = frozenset(
    {"_exportId", "_importId", "branchId", "pageGenerators", "pageProcessors", "routers", "branches"}
)

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

# FIX ROUND 2 (Task 7): NetSuite Restlet Adaptor config on EXPORT steps --
# the export-side counterpart to `_NETSUITE_DA` above, under a DIFFERENT
# top-level key (observed-shapes.md: "imports carry netsuite_da; exports
# carry netsuite -- DIFFERENT KEY FROM IMPORTS"). Task 11's provenance
# ("which flows write which NetSuite record types") needs
# `netsuite.restlet.recordType`/`searchId` for exports the same way it needs
# `netsuite_da.recordType`/`operation` for imports -- Phase D
# (`sync_service.py`) fetches every export already, but without this
# allowlist entry the field never survives sanitize() to reach it. Kept
# narrow on purpose, same discipline as `_NETSUITE_DA`: only the two fields
# Task 11 is known to consume, not the full observed shape --
# `type`/`skipGrouping`/`statsOnly`/`restlet.restletVersion`/
# `restlet.markExportedBatchSize`/`distributed` were observed live too but
# are out of scope for this fix.
_NETSUITE_RESTLET: Schema = {
    "recordType": None,
    "searchId": None,
}
_NETSUITE_EXPORT: Schema = {
    "restlet": _NETSUITE_RESTLET,
}

_INTEGRATION: Schema = {
    "_id": None,
    "name": None,
    "sandbox": None,
    "mode": None,
    "description": None,
    "lastModified": None,
}

# `responseMapping` shape CONFIRMED live, embedded inline inside pageProcessors
# (observed-shapes.md, fix round 3): {fields: [{extract, generate}], lists: []}.
# Config (field-name mapping), not captured data -- Task 5 needs it to build
# celigo_flow_steps, safe to store. `lists` is left out for the same reason
# `netsuite_da.mapping.lists` was in round 1: observed empty, element shape
# genuinely unverified -- dropped rather than guessed at.
_RESPONSE_MAPPING_FIELD: Schema = {
    "extract": None,
    "generate": None,
}
_RESPONSE_MAPPING: Schema = {
    "fields": _RESPONSE_MAPPING_FIELD,
}

# A flow step reference. CONFIRMED live at the flow's top level
# (pageGenerators/pageProcessors) AND, per observed-shapes.md, in the
# IDENTICAL shape inside every router branch ("pageProcessors: [ ...same
# shape as above... ]") -- reused as one schema for both. This IS the flow
# map's topology (fix round 3): Task 5 builds celigo_flow_steps from exactly
# these ids, so dropping them silently empties the map.
#
# FIX ROUND 7: `**_SCRIPT_SITES` here and on every topology node below. It
# was ROUND 4 that added hooks/transform/filter to `_PAGE_PROCESSOR` and
# skipped this, its own sibling -- see `_SCRIPT_SITES`. None of these sites
# is observed live on a page generator (every one probed references its
# export BY ID and inlines nothing), and that is exactly the argument for
# splicing rather than deciding per node: "not observed here" is what
# produced a gap that lost two real refs on the sibling shape.
_PAGE_GENERATOR: Schema = {
    **_SCRIPT_SITES,
    "_exportId": None,
    "skipRetries": None,
}
_PAGE_PROCESSOR: Schema = {
    **_SCRIPT_SITES,
    "type": None,
    "_exportId": None,
    "_importId": None,
    "proceedOnFailure": None,
    "responseMapping": _RESPONSE_MAPPING,
}

# `routers[].branches[]` -- CONFIRMED live, both forms (observed-shapes.md:
# pass-through and branching routers). `nextRouterId` chains routers into a
# graph -- Task 5 needs the chain, not a flat list, so it MUST survive
# alongside `branchId`. `inputFilter` arrives via `_SCRIPT_SITES` (mapped to
# `_FILTER`); it used to be a bare `{rules}` of its own, which dropped a
# `type: "script"` form's attachment.
_ROUTER_BRANCH: Schema = {
    **_SCRIPT_SITES,
    "name": None,
    "branchId": None,
    "nextRouterId": None,
    "pageProcessors": _PAGE_PROCESSOR,
}

# `routers[]` -- CONFIRMED live. `script` is a real attachment site (plan's
# Verified Facts explicitly list "router branches"), but every router
# observed live carries `script: {function: "branching"}` with NO
# `_scriptId` -- every one of those routers used
# `routeRecordsUsing: "input_filters"`, never `"script"`. `script` now
# arrives via `_SCRIPT_SITES` along with its four siblings, for the same
# reason it was kept here on its own before: preserve the ref, never guess
# whether one exists in a fixture.
_ROUTER: Schema = {
    **_SCRIPT_SITES,
    "id": None,
    "name": None,
    "routeRecordsTo": None,
    "routeRecordsUsing": None,
    "branches": _ROUTER_BRANCH,
}

# FIX ROUND 5: `lastModified` was never allowlisted here, so
# `celigo_flows.celigo_last_modified` (migration 094) was permanently NULL --
# the API returns it (observed-shapes.md lists it on the live flow object),
# the schema has the column, `upsert_flow` reads it, and only this allowlist
# was dropping it. Also needed for Task 7's drift detection.
_FLOW: Schema = {
    **_SCRIPT_SITES,
    "_id": None,
    "name": None,
    "_integrationId": None,
    "disabled": None,
    "schedule": None,
    "timezone": None,
    "lastExecutedAt": None,
    "lastModified": None,
    "_sourceId": None,
    "numOpenError": None,
    "lastErrorAt": None,
    "pageGenerators": _PAGE_GENERATOR,
    "pageProcessors": _PAGE_PROCESSOR,
    "routers": _ROUTER,
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
    **_SCRIPT_SITES,
    "_id": None,
    "name": None,
    "adaptorType": None,
    "_connectionId": None,
    "_sourceId": None,
    "sandbox": None,
    "netsuite": _NETSUITE_EXPORT,
    "aiDescription": _AI_DESCRIPTION,
}

# NOTE (fix round 1): the real `GET /v1/imports/{id}` response has no
# top-level `mapping` key -- that was this module's own inferred-not-verified
# guess, and observed-shapes.md proves it wrong. The real mapping config
# lives at `netsuite_da.mapping` (now schema'd above). `filter` IS confirmed
# on import objects and was previously missing here entirely.
#
# NOTE (fix round 4): `transform` was added to `_EXPORT` in round 2 but never
# here -- an import/export asymmetry that was an artifact of what got probed
# (the one live import captured happened to carry `filter`, not `transform`),
# NOT a real API distinction. Celigo imports can carry `transform` too, and
# it's "the regression case... the most-used script in the live account"
# per the plan's Verified Facts -- dropping it on imports silently missed
# the single most common script attachment site for exactly this kind.
_IMPORT: Schema = {
    **_SCRIPT_SITES,
    "_id": None,
    "name": None,
    "adaptorType": None,
    "_connectionId": None,
    "_sourceId": None,
    "sandbox": None,
    "netsuite_da": _NETSUITE_DA,
    "aiDescription": _AI_DESCRIPTION,
}

# FIX ROUND 5: `lastModified` had the same gap as `_FLOW` -- observed live
# (observed-shapes.md's script section), `celigo_scripts.celigo_last_modified`
# column exists (migration 094), `upsert_script` reads it, but nothing here
# allowlisted it.
_SCRIPT: Schema = {
    "_id": None,
    "name": None,
    "content": None,
    "_sourceId": None,
    "sandbox": None,
    "lastModified": None,
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
        if sub_schema is _RULE_TREE:
            # `rules`: an expression tree, filtered by value shape rather
            # than by a key schema -- see `_filter_rule_tree` and fix round 6.
            out[key] = _filter_rule_tree(value)
        elif sub_schema is None:
            # A leaf field: the raw value (whatever its shape) is preserved,
            # but DEEP-copied -- at every depth, not just one level -- so a
            # caller mutating the returned structure can never reach back
            # into *raw* through it. FIX ROUND 8 (whole-branch review finding
            # 9): this used to be a ONE-level-shallow copy
            # (`dict(value)`/`list(value)`), which left any dict/list nested
            # TWO OR MORE levels inside a leaf value (e.g. `schedule`'s own
            # nested `cron` config) aliased to *raw*'s own object --
            # `out["schedule"]["cron"]["x"] = y` still mutated `raw`, which
            # is exactly the shallow-leaf hole finding 3 closed for `rules`
            # (now `_RULE_TREE`, recursively rebuilt) but this general leaf
            # branch still had. `_deep_copy_leaf` gives every leaf field the
            # same full-depth-copy guarantee `_filter_rule_tree` already gives
            # `rules`.
            out[key] = _deep_copy_leaf(value)
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


def _check_depth(depth: int, what: str) -> None:
    """Refuse a structure nested past `_MAX_SANITIZE_DEPTH` -- see that
    constant for the measurement this bound is set against."""
    if depth > _MAX_SANITIZE_DEPTH:
        raise CeligoSanitizeDepthError(
            f"Celigo payload nests more than {_MAX_SANITIZE_DEPTH} levels deep inside {what}; refusing to "
            "walk it further. Either this object is malformed or self-referential, or a real Celigo shape "
            f"has outgrown the limit -- raise {_MAX_SANITIZE_DEPTH} deliberately, with an observed shape to "
            "justify it, rather than removing the bound."
        )


def _deep_copy_leaf(value: object, depth: int = 1) -> object:
    """Deep-copy *value* for a schema LEAF (`sub_schema is None`), at every
    depth, not just the top one -- FIX ROUND 8 (whole-branch review finding
    9). A dict/list value is rebuilt recursively so nothing inside it, at any
    depth, is the same object as *raw*'s; anything else (str/int/float/bool/
    None) is immutable already and returned as-is. Same posture as
    `_filter_rule_tree` for `rules`, generalised to every OTHER leaf field --
    a leaf has no schema to recurse BY, so this recurses by shape alone
    (dict -> dict, list -> list, everything else verbatim), never dropping a
    key the way a real schema-driven filter would.

    Bounded by `_MAX_SANITIZE_DEPTH` (FIX ROUND 9): recursing by shape means
    the input decides the depth, so the input must not be able to decide it
    without limit."""
    if isinstance(value, dict):
        _check_depth(depth, "an allowlisted leaf field")
        return {key: _deep_copy_leaf(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        _check_depth(depth, "an allowlisted leaf field")
        return [_deep_copy_leaf(item, depth + 1) for item in value]
    return value


def _filter_rule_tree(value: object, depth: int = 1) -> object:
    """Recursively filter one Celigo EXPRESSION TREE (`rules`) by value shape.

    Fix round 6 -- see `_RULE_DICT_KEYS` above for why `rules` gets this
    instead of a key schema, and for the two real tree shapes it must leave
    byte-identical.

    * list  -- rebuilt element by element, every element filtered. Nothing is
      dropped for being the "wrong" type: `["notempty", ["string", [...]]]`
      is a real, legitimate tree of bare strings, and a filter that only
      understood dicts would gut it.
    * dict  -- keys not in `_RULE_DICT_KEYS` are DROPPED, and the values of
      the ones that survive are filtered again. This is the whole privacy
      control: a captured payload is always a dict under some key, at some
      depth, so an allowlist applied at EVERY depth is what closes the hole
      a one-level-shallow leaf copy left open.
    * str/int/float/bool/None -- returned as-is. These are operators and
      operands; none can hide a nested capture.
    * anything else -- dropped (returned as None). Unreachable from parsed
      JSON, kept fail-closed anyway, same posture as `_apply_schema`'s final
      `else`.

    Builds new containers throughout, never edits *value* -- `sanitize()`'s
    purity contract holds through this path too.

    Bounded by `_MAX_SANITIZE_DEPTH` (FIX ROUND 9), for the same reason as
    `_deep_copy_leaf`: the tree's shape comes entirely from the input.
    """
    if isinstance(value, dict):
        _check_depth(depth, "a `rules` expression tree")
        return {key: _filter_rule_tree(item, depth + 1) for key, item in value.items() if key in _RULE_DICT_KEYS}
    if isinstance(value, list):
        _check_depth(depth, "a `rules` expression tree")
        return [_filter_rule_tree(item, depth + 1) for item in value]
    if value is None or isinstance(value, (str, int, float)):  # bool is an int subclass
        return value
    return None
