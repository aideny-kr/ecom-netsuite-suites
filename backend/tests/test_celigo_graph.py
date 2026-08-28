"""Tests for the recursive script-reference walker.

See docs/superpowers/specs/2026-08-25-celigo-flow-map-design.md and
.superpowers/sdd/2026-08-25-celigo-plan-b-flow-map/observed-shapes.md.

The core design point (plan's Verified Facts): scripts attach at
`transform.script`, `filter.script`, `hooks.*`, and router branches -- but
that is NOT a fixed site list to enumerate. The walker must find a
`_scriptId` wherever it occurs, recursively, so a new/unseen attachment site
is found automatically. Fixtures below use INVENTED ids -- shape only, no
real Celigo/production data (per this session's ruling, see sanitizer tests).

Two real negative cases, both live-confirmed (observed-shapes.md): a
`filter`/`transform` of `type: "expression"` carries no `_scriptId` and must
yield no ref. The walker must key on the presence of `_scriptId` itself,
never on the `filter`/`transform` key or on `type`.
"""

from __future__ import annotations

from app.services.celigo.graph import ScriptRef, walk_script_refs


class TestFindsEveryKnownScriptAttachmentSite:
    """The four Step-1 fixtures, each a real observed shape (or, for the
    router case, a documented hypothetical -- see that test's docstring)."""

    def test_transform_script_type_is_found(self):
        """THE regression case (plan's Verified Facts): the most-used script
        in the live account attaches here, and a hooks-only search misses it."""
        obj = {
            "_id": "e1",
            "transform": {
                "type": "script",
                "script": {"_scriptId": "scr-transform-1", "function": "mapFields"},
            },
        }
        refs = walk_script_refs(obj)
        assert refs == [
            ScriptRef(
                script_id="scr-transform-1",
                function_name="mapFields",
                json_path="transform.script",
                site_type="transform",
            )
        ]

    def test_hook_script_is_found(self):
        obj = {
            "_id": "i1",
            "hooks": {"preSavePage": {"_scriptId": "scr-hook-1", "function": "beforeSave"}},
        }
        refs = walk_script_refs(obj)
        assert refs == [
            ScriptRef(
                script_id="scr-hook-1",
                function_name="beforeSave",
                json_path="hooks.preSavePage",
                site_type="hook",
            )
        ]

    def test_filter_script_type_is_found_with_no_function_key(self):
        """Real shape can carry `_scriptId` with no `function` key at all --
        must not KeyError; `function_name` is None rather than a default string."""
        obj = {"_id": "i1", "filter": {"type": "script", "script": {"_scriptId": "scr-filter-1"}}}
        refs = walk_script_refs(obj)
        assert refs == [
            ScriptRef(
                script_id="scr-filter-1",
                function_name=None,
                json_path="filter.script",
                site_type="filter",
            )
        ]

    def test_router_branch_script_is_found(self):
        """HYPOTHETICAL, NOT OBSERVED LIVE. observed-shapes.md: every router
        probed across 60 real flows carries `routers[].script:
        {function: "branching"}` with NO `_scriptId` (all used
        `routeRecordsUsing: "input_filters"`, never `"script"`). This fixture
        does not assert that real shape carries a script id -- it proves the
        walker reaches INTO a router branch (an arbitrary nesting depth) and
        finds a `_scriptId` there without `branches`/`routers` being a
        hardcoded special case in the walker itself."""
        obj = {
            "_id": "f1",
            "routers": [
                {
                    "id": "rtr-A",
                    "branches": [
                        {
                            "branchId": "brn-A1",
                            "script": {"_scriptId": "scr-router-1", "function": "custom"},
                        }
                    ],
                }
            ],
        }
        refs = walk_script_refs(obj)
        assert refs == [
            ScriptRef(
                script_id="scr-router-1",
                function_name="custom",
                json_path="routers[0].branches[0].script",
                site_type="router",
            )
        ]


class TestNegativeCasesYieldNoRef:
    """Both REAL, live-confirmed (observed-shapes.md import/export sections).
    Keying on the `filter`/`transform` key or on `type` would wrongly
    manufacture a ref here -- only `_scriptId`'s presence may decide."""

    def test_expression_type_filter_yields_no_ref(self):
        obj = {
            "_id": "i1",
            "filter": {
                "type": "expression",
                "expression": {"rules": [{"field": "status", "op": "eq"}], "version": "1"},
                "rules": [{"field": "status", "op": "eq"}],
                "version": "1",
            },
        }
        assert walk_script_refs(obj) == []

    def test_expression_type_transform_yields_no_ref(self):
        obj = {
            "_id": "e1",
            "transform": {
                "type": "expression",
                "expression": {"version": "1", "rules": [[{"key": "a", "extract": "b", "generate": "c"}]]},
                "rules": [[{"key": "a", "extract": "b", "generate": "c"}]],
                "version": "1",
            },
        }
        assert walk_script_refs(obj) == []

    def test_object_with_no_script_refs_yields_empty_list(self):
        obj = {"_id": "f1", "name": "No scripts here", "disabled": False, "schedule": "0 * * * *"}
        assert walk_script_refs(obj) == []


class TestFindsUnenumeratedAttachmentSites:
    """The core design point under direct test: the walk finds `_scriptId`
    wherever it occurs -- a structure nobody has seen, several levels deep
    inside keys the walker has never heard of, must still be found. If this
    ever required a code change to recognize, the walker would have silently
    regressed into an enumerated hook list."""

    def test_deeply_nested_unknown_structure_is_still_found(self):
        obj = {
            "_id": "f1",
            "someBrandNewCeligoFeatureNeverSeenBefore": {
                "nestedAgain": {
                    "evenDeeper": [
                        {"_scriptId": "scr-unknown-1", "function": "surpriseHandler"},
                    ]
                }
            },
        }
        refs = walk_script_refs(obj)
        assert refs == [
            ScriptRef(
                script_id="scr-unknown-1",
                function_name="surpriseHandler",
                json_path="someBrandNewCeligoFeatureNeverSeenBefore.nestedAgain.evenDeeper[0]",
                site_type="unknown",
            )
        ]

    def test_multiple_refs_across_different_sites_are_all_found(self):
        obj = {
            "_id": "f1",
            "transform": {"type": "script", "script": {"_scriptId": "scr-a", "function": "fnA"}},
            "hooks": {"preSavePage": {"_scriptId": "scr-b", "function": "fnB"}},
        }
        refs = walk_script_refs(obj)
        assert {(r.script_id, r.site_type) for r in refs} == {("scr-a", "transform"), ("scr-b", "hook")}
        assert len(refs) == 2

    def test_script_id_at_the_top_level_is_found(self):
        """Edge case: no nesting at all. json_path falls back to a root
        marker rather than an empty string or crashing."""
        obj = {"_scriptId": "scr-root-1", "function": "topLevelFn"}
        refs = walk_script_refs(obj)
        assert refs == [
            ScriptRef(script_id="scr-root-1", function_name="topLevelFn", json_path="$", site_type="unknown")
        ]


class TestMalformedScriptIdIsNotARef:
    """A `_scriptId` is only ever a non-empty Mongo ObjectId string in every
    observed shape. Guard against a malformed/placeholder value manufacturing
    a bogus ref rather than being silently skipped."""

    def test_empty_string_script_id_is_not_a_ref(self):
        obj = {"_id": "i1", "filter": {"script": {"_scriptId": ""}}}
        assert walk_script_refs(obj) == []

    def test_none_script_id_is_not_a_ref(self):
        obj = {"_id": "i1", "filter": {"script": {"_scriptId": None}}}
        assert walk_script_refs(obj) == []
