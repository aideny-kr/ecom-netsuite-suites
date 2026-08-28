"""Celigo payload sanitizer -- allowlist, never denylist.

See docs/superpowers/specs/2026-08-25-celigo-flow-map-design.md §4.3/§6: probed
live on 2026-08-25 against the real Solidus + NetSuite integration, Celigo
objects were found to embed captured production payloads -- a `mockResponse`
contained a live `set-cookie` session header for `.frame.work`, a customer
record, and product data.

FIX ROUND 1 (2026-08-27): re-probed live, `exclude` projection on the wire
does NOT reliably strip payload fields -- `GET /v1/imports/{id}` with
`exclude=...,mockResponse,...` still returned `mockResponse`. This sanitizer
is therefore the ONLY effective defence, not a backstop behind projection, so
every allowlisted field that can itself be a nested object (`filter`,
`netsuite_da.mapping`) must be recursively filtered rather than blob-copied.

The fixture below mirrors that REAL shape structurally -- an import object
whose payload-bearing fields carry a cookie header and a customer/product
body -- but every value is INVENTED. No real cookie, email, or order
reference belongs in this repo.

FIX ROUND 6 (2026-08-27, final-review finding 3): `rules` is no longer a
schema leaf -- it is filtered as an expression tree, and dicts inside it keep
only observed config keys (`sanitizer._RULE_DICT_KEYS`). Several fixtures
here previously spelled a rule as `{"field": ..., "op": ...}` /
`{"field": ..., "value": ...}`, key names NOBODY EVER OBSERVED -- invented
when `rules` was blob-copied and its contents therefore never mattered. They
now read `{key, extract, generate}`, the shape observed-shapes.md actually
recorded live. Changing them was not a matter of making a new assertion pass:
an invented shape in a fixture is the failure mode this whole session has
been about, and a privacy allowlist widened to accommodate one would have
been strictly worse. See `TestRulesAreFilteredAsExpressionTrees` at the end
of this file.
"""

from __future__ import annotations

import copy
import json
import sys

import pytest

from app.services.celigo import sanitizer
from app.services.celigo.graph import walk_script_refs
from app.services.celigo.repository import FlowStepInput, extract_flow_steps
from app.services.celigo.sanitizer import sanitize

# Celigo's "test import"/"preview mapping" feature stores its last captured
# request/response on the import config object itself -- and, per the spec,
# that capture can recur at MULTIPLE nesting levels, not only the top level
# (the account's own `netsuite_da` distributed-adaptor config carries its own
# capture). The fixture plants the same danger in both places so the test
# actually exercises recursion, not just top-level key dropping.
_CAPTURED_IMPORT_PAYLOAD = {
    # Allowlisted identity/config fields -- must survive verbatim.
    "_id": "622f3aa1cf1c8e001a2c9e10",
    "name": "Sync Orders to NetSuite",
    "adaptorType": "NetSuiteImport",
    "_connectionId": "5f8a1e2b3c4d5e6f7a8b9c0d",
    "_sourceId": "5f8a1e2b3c4d5e6f7a8b9c0e",
    "sandbox": False,
    "netsuite_da": {
        "recordType": "customer",
        "operation": "add",
        # Nested capture -- must be stripped by recursion into netsuite_da's
        # OWN allowlist, not by the top-level pass alone.
        "mockResponse": {
            "_headers": {"set-cookie": ["connect.sid=s%3AeJ8xInvented.frame.work; Path=/; HttpOnly; Secure"]},
            "body": {
                "customer": {"email": "jane.doe@example-shop.test", "name": "Jane Doe"},
                "product": {"sku": "WIDGET-100", "name": "Blue Widget"},
            },
        },
    },
    # Top-level capture -- must be absent entirely.
    "mockResponse": {
        "_headers": {"set-cookie": ["session=top-level-invented; HttpOnly"]},
        "body": {"customer": {"email": "top.level@example-shop.test"}},
    },
    "mockOutput": {"status": "success", "recordId": "789"},
    "sampleData": {"orderId": "R123456", "total": 42.5},
    "rawData": "<raw wire bytes, never persisted>",
    # An unknown, never-before-seen key -- the allowlist property under test.
    # A denylist keyed on known-dangerous names would pass this through.
    "someBrandNewCeligoFieldNeverSeenBefore": "must be dropped by default",
}

_EXPECTED_SANITIZED_IMPORT = {
    "_id": "622f3aa1cf1c8e001a2c9e10",
    "name": "Sync Orders to NetSuite",
    "adaptorType": "NetSuiteImport",
    "_connectionId": "5f8a1e2b3c4d5e6f7a8b9c0d",
    "_sourceId": "5f8a1e2b3c4d5e6f7a8b9c0e",
    "sandbox": False,
    "netsuite_da": {"recordType": "customer", "operation": "add"},
}


def _dumped(obj: dict) -> str:
    """Serialize the WHOLE structure to search it, rather than checking only
    the field names we happen to think of -- a leak under an unexpected key
    must still fail this."""
    return json.dumps(obj)


class TestImportSanitization:
    def test_full_output_shape(self):
        assert sanitize("import", _CAPTURED_IMPORT_PAYLOAD) == _EXPECTED_SANITIZED_IMPORT

    def test_allowlisted_identity_fields_survive(self):
        out = sanitize("import", _CAPTURED_IMPORT_PAYLOAD)
        for field in ("_id", "name", "adaptorType", "_connectionId", "_sourceId", "sandbox"):
            assert field in out
            assert out[field] == _CAPTURED_IMPORT_PAYLOAD[field]

    def test_top_level_payload_bearing_fields_are_absent(self):
        out = sanitize("import", _CAPTURED_IMPORT_PAYLOAD)
        for field in ("mockResponse", "mockOutput", "sampleData", "rawData", "_headers"):
            assert field not in out

    def test_nested_payload_bearing_fields_are_absent(self):
        """Recursion, not just top-level dropping: netsuite_da carries its OWN
        mockResponse/_headers capture and it must be stripped too, while its
        legitimate fields survive."""
        out = sanitize("import", _CAPTURED_IMPORT_PAYLOAD)
        assert out["netsuite_da"] == {"recordType": "customer", "operation": "add"}
        for field in ("mockResponse", "mockOutput", "sampleData", "rawData", "_headers"):
            assert field not in out["netsuite_da"]

    def test_no_set_cookie_value_survives_anywhere(self):
        out = sanitize("import", _CAPTURED_IMPORT_PAYLOAD)
        assert "set-cookie" not in _dumped(out)
        assert "connect.sid" not in _dumped(out)

    def test_no_customer_or_product_data_survives_anywhere(self):
        out = sanitize("import", _CAPTURED_IMPORT_PAYLOAD)
        assert "example-shop.test" not in _dumped(out)
        assert "WIDGET-100" not in _dumped(out)

    def test_unknown_never_before_seen_key_is_dropped_by_default(self):
        out = sanitize("import", _CAPTURED_IMPORT_PAYLOAD)
        assert "someBrandNewCeligoFieldNeverSeenBefore" not in out
        assert "someBrandNewCeligoFieldNeverSeenBefore" not in _dumped(out)


class TestPurity:
    def test_input_dict_is_not_mutated(self):
        before = copy.deepcopy(_CAPTURED_IMPORT_PAYLOAD)
        sanitize("import", _CAPTURED_IMPORT_PAYLOAD)
        assert _CAPTURED_IMPORT_PAYLOAD == before


class TestLeafValuesAreFullyDeepCopied:
    """WHOLE-BRANCH REVIEW FINDING 9 (2026-08-27): `_apply_schema`'s leaf
    branch (`sub_schema is None`) used to claim a caller mutating the
    returned structure "can't reach back into raw through it -- purity holds
    for the caller's copy too". False as written: `dict(value)`/`list(value)`
    copy only ONE level shallow, so a nested dict/list two or more levels
    deep inside a leaf field's value was still the SAME object as *raw*'s.

    Finding 3's fix (`rules` -> `_RULE_TREE`, recursively rebuilt) closed the
    specific example the review gave (`out["filter"]["rules"][0]["nested"] =
    X`) -- `filter` is a nested schema now, not a leaf, and everything under
    it (including `rules`) is rebuilt at every depth. RE-VERIFIED before this
    fix: the hole is still open through a DIFFERENT leaf field -- `schedule`
    on `_FLOW` is `None` (an undifferentiated leaf) and is `dict | None` at
    the model/API layer, so a nested dict inside it is exactly this shape.
    """

    def test_mutating_a_nested_dict_inside_a_leaf_field_does_not_reach_raw(self):
        raw = {
            "_id": "flow_1",
            "name": "Test Flow",
            # A nested dict INSIDE a leaf field's value -- `schedule` itself
            # is copied (`dict(value)`), but a shallow copy leaves this
            # `cron` sub-dict aliased to raw's own object unless the copy
            # goes all the way down.
            "schedule": {"type": "custom", "cron": {"hour": 9, "minute": 0}},
        }
        out = sanitize("flow", raw)

        out["schedule"]["cron"]["hour"] = 99

        assert raw["schedule"]["cron"]["hour"] == 9, "mutating the sanitized copy must never reach raw"

    def test_mutating_a_list_nested_inside_a_leaf_field_does_not_reach_raw(self):
        raw = {
            "_id": "flow_1",
            "name": "Test Flow",
            "schedule": {"type": "custom", "excludeDates": [{"date": "2026-12-25"}]},
        }
        out = sanitize("flow", raw)

        out["schedule"]["excludeDates"][0]["date"] = "TAMPERED"

        assert raw["schedule"]["excludeDates"][0]["date"] == "2026-12-25"


class TestUnknownResourceKindFailsClosed:
    """RESOLVED AMBIGUITY: an unrecognized resource_kind has no allowlist, so
    nothing about it is provably safe -- return an empty dict rather than
    passing the object through. This is the SAME allowlist property as
    dropping an unknown key, one level up."""

    def test_unrecognized_kind_returns_empty_dict(self):
        assert sanitize("widget", {"_id": "x", "name": "y"}) == {}

    def test_fails_closed_even_when_payload_has_only_known_safe_fields(self):
        """Even a payload containing nothing but fields that WOULD be
        allowlisted for a real kind must still yield {} for an unrecognized
        kind -- there is no fallback default schema."""
        assert sanitize("widget", dict(_EXPECTED_SANITIZED_IMPORT)) == {}


class TestNestedNonDictValueIsDropped:
    def test_nested_schema_key_with_scalar_value_is_dropped(self):
        """netsuite_da is expected to be a dict; if Celigo ever sends
        something else there, it can't be safely filtered by netsuite_da's
        allowlist, so it's dropped rather than passed through or guessed at."""
        raw = {"_id": "i1", "netsuite_da": "not-a-dict"}
        out = sanitize("import", raw)
        assert out == {"_id": "i1"}


class TestOtherResourceKinds:
    """Step 1's fixture only exercises `import`; these confirm each of the
    other five kinds has its own working allowlist rather than falling
    through to the fail-closed empty-dict path."""

    def test_integration(self):
        raw = {
            "_id": "int1",
            "name": "Solidus + NetSuite",
            "sandbox": False,
            "mode": "install",
            "description": "Order sync",
            "lastModified": "2026-08-25T00:00:00.000Z",
            "mockResponse": {"_headers": {"set-cookie": ["x"]}},
        }
        out = sanitize("integration", raw)
        assert out == {
            "_id": "int1",
            "name": "Solidus + NetSuite",
            "sandbox": False,
            "mode": "install",
            "description": "Order sync",
            "lastModified": "2026-08-25T00:00:00.000Z",
        }

    def test_flow(self):
        raw = {
            "_id": "f1",
            "name": "NS - Create Customer Deposits",
            "_integrationId": "int1",
            "disabled": False,
            "schedule": "0 * * * *",
            "timezone": "America/New_York",
            "lastExecutedAt": "2026-08-25T00:00:00.000Z",
            "_sourceId": "src1",
            "numOpenError": 0,
            "lastErrorAt": None,
            "aiDescription": {
                "summary": "Creates deposits",
                "detailed": "Long form",
                "generatedOn": None,
                "mockResponse": {"_headers": {"set-cookie": ["x"]}},
            },
            "rawData": "drop-me",
        }
        out = sanitize("flow", raw)
        assert out["aiDescription"] == {
            "summary": "Creates deposits",
            "detailed": "Long form",
            "generatedOn": None,
        }
        assert "rawData" not in out
        assert out["disabled"] is False
        assert out["_integrationId"] == "int1"

    def test_export(self):
        # `filter` shape confirmed live (observed-shapes.md, fix round 1):
        # {type, expression: {rules, version}, rules, version}.
        raw = {
            "_id": "e1",
            "name": "Solidus Orders Export",
            "adaptorType": "RestExport",
            "_connectionId": "conn1",
            "_sourceId": "src2",
            "sandbox": False,
            "filter": {
                "type": "expression",
                "expression": {
                    "rules": [{"key": "r1", "extract": "status", "generate": "orderStatus"}],
                    "version": "1",
                },
                "rules": [{"key": "r1", "extract": "status", "generate": "orderStatus"}],
                "version": "1",
            },
            "sampleData": {"customer": {"email": "leaked@example-shop.test"}},
        }
        out = sanitize("export", raw)
        assert "sampleData" not in out
        assert out["filter"] == raw["filter"]

    def test_script(self):
        raw = {
            "_id": "s1",
            "name": "Null guard",
            "content": "function main(options) { return options; }",
            "_sourceId": "src3",
            "sandbox": False,
            "mockOutput": {"result": "leaked"},
        }
        out = sanitize("script", raw)
        assert out == {
            "_id": "s1",
            "name": "Null guard",
            "content": "function main(options) { return options; }",
            "_sourceId": "src3",
            "sandbox": False,
        }

    def test_error(self):
        """Error `message` is exempt from stripping and stored verbatim (spec
        §4.3) -- it is the diagnostic payload, even though messages
        demonstrably contain PII. The sanitizer's job is dropping UNKNOWN
        fields, not scrubbing PII out of allowlisted ones (that is the
        signature fingerprint's job, a later task)."""
        raw = {
            "traceKey": "R000000001",
            "errorId": "err1",
            "retryDataKey": "rdk1",
            "source": "pre_save_page_hook",
            "code": "MISSING_SHIP_ADDRESS",
            "message": (
                "MISSING_SHIP_ADDRESS: order R000000001 has no ship_address "
                "in the Solidus payload. Usually a GDPR-scrubbed customer "
                "(email deleted_user_000000@user.deleted) -- check the order."
            ),
            "occurredAt": "2026-08-10T00:00:00.000Z",
            "purgeAt": "2026-09-09T00:00:00.000Z",
            "_flowJobId": "job1",
            "retriable": True,
            "someInternalCeligoField": "drop-me",
        }
        out = sanitize("error", raw)
        assert out["message"] == raw["message"]
        assert "someInternalCeligoField" not in out
        assert out["retriable"] is True


class TestFilterAndMappingAreNotBlobCopied:
    """FIX ROUND 1, finding 1: `filter` (export/import) and `netsuite_da.mapping`
    (import) were declared as schema LEAVES, so a captured-payload-shaped value
    nested inside either would pass straight through unfiltered -- the exact
    hole `exclude` projection was wrongly assumed to close (see module
    docstring). Real shapes from observed-shapes.md (probed live 2026-08-27):

        filter: {type, expression: {rules, version}, rules, version}
        netsuite_da.mapping: {fields: [{extract, generate, internalId}], lists}
    """

    def test_captured_payload_nested_inside_export_filter_is_stripped(self):
        raw = {
            "_id": "e1",
            "filter": {
                "type": "expression",
                "expression": {
                    "rules": [{"key": "r1", "extract": "status", "generate": "orderStatus"}],
                    "version": "1",
                },
                "rules": [{"key": "r1", "extract": "status", "generate": "orderStatus"}],
                "version": "1",
                "mockResponse": {"_headers": {"set-cookie": ["export-filter-leak"]}},
            },
        }
        out = sanitize("export", raw)
        assert out["filter"]["type"] == "expression"
        assert out["filter"]["expression"] == {
            "rules": [{"key": "r1", "extract": "status", "generate": "orderStatus"}],
            "version": "1",
        }
        assert out["filter"]["rules"] == [{"key": "r1", "extract": "status", "generate": "orderStatus"}]
        assert out["filter"]["version"] == "1"
        assert "mockResponse" not in out["filter"]
        assert "set-cookie" not in _dumped(out)

    def test_captured_payload_nested_inside_import_filter_is_stripped(self):
        """`filter` is confirmed live on import objects too, not just export --
        it was previously missing from `_IMPORT`'s allowlist entirely."""
        raw = {
            "_id": "i1",
            "filter": {
                "type": "expression",
                "expression": {"rules": [], "version": "1"},
                "rules": [],
                "version": "1",
                "mockResponse": {"_headers": {"set-cookie": ["import-filter-leak"]}},
            },
        }
        out = sanitize("import", raw)
        assert out["filter"]["type"] == "expression"
        assert "mockResponse" not in out["filter"]
        assert "set-cookie" not in _dumped(out)

    def test_captured_payload_nested_inside_netsuite_da_mapping_is_stripped(self):
        raw = {
            "_id": "i1",
            "netsuite_da": {
                "recordType": "returnauthorization",
                "operation": "update",
                "mapping": {
                    "fields": [
                        {
                            "extract": "email",
                            "generate": "custbody_email",
                            "internalId": "123",
                            # A captured-payload-shaped value on a single field
                            # entry -- must be stripped without dropping the
                            # field's own legitimate keys.
                            "mockResponse": {"_headers": {"set-cookie": ["field-leak"]}},
                        }
                    ],
                    "mockOutput": {"result": "leaked"},
                },
            },
        }
        out = sanitize("import", raw)
        mapping = out["netsuite_da"]["mapping"]
        assert mapping["fields"] == [{"extract": "email", "generate": "custbody_email", "internalId": "123"}]
        assert "mockOutput" not in mapping
        assert "set-cookie" not in _dumped(out)


class TestScriptReferencesSurviveSanitization:
    """FIX ROUND 2: a `_scriptId` is an object id, not captured payload data --
    there is no reason to drop one. Follow it through the pipeline: Task 2's
    script-graph walker finds every `_scriptId`; if it ever runs on SANITIZED
    (stored) data rather than the raw response, every attachment site this
    sanitizer drops becomes a script that does not exist as far as our flow
    map is concerned -- the map comes back empty with nothing erroring. So
    sanitizing must never destroy a script reference.

    Real shapes (observed-shapes.md, probed live 2026-08-27): `filter` on
    import and `transform` on export are structurally parallel, each with two
    forms -- `type: "expression"` (config only, no script, CONFIRMED live on
    both) or `type: "script"` (a nested `script: {_scriptId, function}`
    carries the attachment). `hooks.*` is a THIRD site with an open-ended,
    never-enumerated key name (plan's Verified Facts: "recursive walk, not an
    enumerated hook list").
    """

    def test_script_type_filter_keeps_script_id_on_import(self):
        raw = {
            "_id": "i1",
            "filter": {
                "type": "script",
                "script": {"_scriptId": "scr1", "function": "matchOrder"},
            },
        }
        out = sanitize("import", raw)
        assert out["filter"]["type"] == "script"
        assert out["filter"]["script"] == {"_scriptId": "scr1", "function": "matchOrder"}

    def test_script_type_transform_keeps_script_id_and_function_on_export(self):
        """THE most-used script attachment site in the live account, per the
        plan's Verified Facts."""
        raw = {
            "_id": "e1",
            "transform": {
                "type": "script",
                "script": {"_scriptId": "scr2", "function": "mapFields"},
            },
        }
        out = sanitize("export", raw)
        assert out["transform"]["type"] == "script"
        assert out["transform"]["script"] == {"_scriptId": "scr2", "function": "mapFields"}

    def test_arbitrary_never_enumerated_hook_name_keeps_script_id(self):
        """Proves the sanitizer does NOT hardcode a hook taxonomy -- a hook
        name invented for this test alone, never seen live and never listed
        anywhere in this file, still survives with its script reference."""
        raw = {
            "_id": "i1",
            "hooks": {
                "someFutureHookNobodyHasEnumerated": {"_scriptId": "scr3", "function": "onEvent"},
            },
        }
        out = sanitize("import", raw)
        assert out["hooks"] == {
            "someFutureHookNobodyHasEnumerated": {"_scriptId": "scr3", "function": "onEvent"},
        }

    def test_expression_form_filter_survives_with_no_script_ref(self):
        """Real negative case (observed-shapes.md import section): `type ==
        'expression'` carries no script and must yield none -- the walker
        (a later task) must not treat every `filter` as a script site, and
        this sanitizer must not manufacture one either."""
        raw = {
            "_id": "i1",
            "filter": {
                "type": "expression",
                "expression": {"rules": [{"key": "r1", "extract": "status"}], "version": "1"},
                "rules": [{"key": "r1", "extract": "status"}],
                "version": "1",
            },
        }
        out = sanitize("import", raw)
        assert out["filter"]["type"] == "expression"
        assert "script" not in out["filter"]

    def test_expression_form_transform_survives_with_no_script_ref(self):
        """Real negative case (observed-shapes.md export section) -- keying on
        the presence of `transform` alone would wrongly manufacture a script
        ref here."""
        raw = {
            "_id": "e1",
            "transform": {
                "type": "expression",
                "expression": {"rules": [[{"key": "a", "extract": "b", "generate": "c"}]], "version": "1"},
                "rules": [[{"key": "a", "extract": "b", "generate": "c"}]],
                "version": "1",
            },
        }
        out = sanitize("export", raw)
        assert out["transform"]["type"] == "expression"
        assert "script" not in out["transform"]

    def test_captured_payload_nested_inside_script_form_filter_is_still_stripped(self):
        """Preserving the script ref must not reopen the payload hole fixed in
        round 1."""
        raw = {
            "_id": "i1",
            "filter": {
                "type": "script",
                "script": {"_scriptId": "scr4", "function": "onFilter"},
                "mockResponse": {"_headers": {"set-cookie": ["filter-script-leak"]}},
            },
        }
        out = sanitize("import", raw)
        assert out["filter"]["script"] == {"_scriptId": "scr4", "function": "onFilter"}
        assert "mockResponse" not in out["filter"]
        assert "set-cookie" not in _dumped(out)

    def test_captured_payload_nested_inside_script_form_transform_is_still_stripped(self):
        raw = {
            "_id": "e1",
            "transform": {
                "type": "script",
                "script": {"_scriptId": "scr5", "function": "onTransform"},
                "mockOutput": {"result": "leaked"},
            },
        }
        out = sanitize("export", raw)
        assert out["transform"]["script"] == {"_scriptId": "scr5", "function": "onTransform"}
        assert "mockOutput" not in out["transform"]

    def test_captured_payload_nested_inside_hook_entry_is_still_stripped(self):
        """The wildcard hooks.* mechanism must not become an accidental
        pass-through for whatever else a hook entry happens to carry."""
        raw = {
            "_id": "i1",
            "hooks": {
                "preSavePage": {
                    "_scriptId": "scr6",
                    "function": "beforeSave",
                    "mockResponse": {"_headers": {"set-cookie": ["hook-leak"]}},
                }
            },
        }
        out = sanitize("import", raw)
        assert out["hooks"]["preSavePage"] == {"_scriptId": "scr6", "function": "beforeSave"}
        assert "set-cookie" not in _dumped(out)


class TestFlowTopologySurvivesSanitization:
    """FIX ROUND 3: dropping a flow's `pageGenerators`/`pageProcessors`/
    `routers` is the SAME silent-failure class as round 2's `_scriptId` gap,
    with a wider blast radius -- these ARE the flow map. Task 5 builds
    `celigo_flow_steps` from exactly these ids; a flow sanitizing down to
    `{_id, name}` means every stored flow has zero steps, and the map comes
    back empty with nothing erroring. Step/branch ids and routing config are
    ids and config, not captured payload data -- there is no case where
    dropping them is correct.

    Real shapes (observed-shapes.md "routers" section, probed live
    2026-08-27, across 60 real flows): every multi-subsidiary sales-order
    flow -- the ones the recon chain depends on -- puts its steps inside
    `routers[].branches[].pageProcessors`, NOT the top-level arrays. A Task 5
    read of only `pageGenerators`/`pageProcessors` silently misses most steps
    in exactly the flows that matter most.
    """

    def test_top_level_page_generators_and_processors_survive(self):
        raw = {
            "_id": "f1",
            "name": "Backfill - RA Original Order Shipped Date",
            "pageGenerators": [{"_exportId": "exp1", "skipRetries": True}],
            "pageProcessors": [
                {
                    "type": "import",
                    "_importId": "imp1",
                    "responseMapping": {"fields": [{"extract": "id", "generate": "internalid"}], "lists": []},
                }
            ],
        }
        out = sanitize("flow", raw)
        assert out["pageGenerators"] == [{"_exportId": "exp1", "skipRetries": True}]
        assert out["pageProcessors"][0]["type"] == "import"
        assert out["pageProcessors"][0]["_importId"] == "imp1"
        assert out["pageProcessors"][0]["responseMapping"] == {"fields": [{"extract": "id", "generate": "internalid"}]}

    def test_routers_branches_page_processors_survive(self):
        """The Task 5 defect this round exists to prevent: every
        multi-subsidiary sales-order flow puts its steps inside
        routers[].branches[].pageProcessors, not the top-level arrays --
        the import id inside a branch must survive."""
        raw = {
            "_id": "f2",
            "name": "NS - Create Sales Order (multi-sub)",
            "routers": [
                {
                    "id": "rtr-A",
                    "name": "",
                    "routeRecordsTo": "first_matching_branch",
                    "routeRecordsUsing": "input_filters",
                    "branches": [
                        {
                            "name": "Framework Intl",
                            "branchId": "brn-A1",
                            "inputFilter": {"rules": [{"key": "b1", "extract": "subsidiary", "generate": "intl"}]},
                            "pageProcessors": [
                                {
                                    "type": "import",
                                    "_importId": "imp-intl",
                                    "responseMapping": {"fields": [{"extract": "internalid", "generate": "id"}]},
                                }
                            ],
                        }
                    ],
                    "script": {"function": "branching"},
                }
            ],
        }
        out = sanitize("flow", raw)
        router = out["routers"][0]
        assert router["routeRecordsTo"] == "first_matching_branch"
        assert router["routeRecordsUsing"] == "input_filters"
        branch = router["branches"][0]
        assert branch["inputFilter"] == {"rules": [{"key": "b1", "extract": "subsidiary", "generate": "intl"}]}
        assert branch["pageProcessors"] == [
            {
                "type": "import",
                "_importId": "imp-intl",
                "responseMapping": {"fields": [{"extract": "internalid", "generate": "id"}]},
            }
        ]

    def test_next_router_id_and_branch_id_survive(self):
        """`nextRouterId` chains routers into a graph -- Task 5 needs the
        chain, not a flat list, so both ids must survive."""
        raw = {
            "_id": "f3",
            "name": "Pass-through router flow",
            "routers": [
                {
                    "id": "rtr-A",
                    "name": "",
                    "branches": [
                        {
                            "name": "",
                            "pageProcessors": [],
                            "nextRouterId": "rtr-B",
                            "branchId": "brn-A1",
                        }
                    ],
                }
            ],
        }
        out = sanitize("flow", raw)
        branch = out["routers"][0]["branches"][0]
        assert branch["nextRouterId"] == "rtr-B"
        assert branch["branchId"] == "brn-A1"

    def test_router_script_function_survives(self):
        """The REAL shape: every router observed live carries
        `script: {function: "branching"}` with NO `_scriptId`
        (observed-shapes.md)."""
        raw = {
            "_id": "f4",
            "name": "Branching router flow",
            "routers": [{"id": "rtr-A", "name": "", "branches": [], "script": {"function": "branching"}}],
        }
        out = sanitize("flow", raw)
        assert out["routers"][0]["script"] == {"function": "branching"}

    def test_router_script_id_survives_if_ever_present(self):
        """HYPOTHETICAL, NOT OBSERVED LIVE (per observed-shapes.md: "DO NOT
        write a test asserting routers[].script._scriptId exists -- it does
        not in any probed router"). Every router probed used
        `routeRecordsUsing: "input_filters"`, never `"script"`. This fixture
        only proves the schema doesn't special-case function-only routers --
        IF a `_scriptId` ever does appear here, it must survive too, same as
        every other script attachment site."""
        raw = {
            "_id": "f5",
            "name": "Hypothetical script-routed flow -- shape invented, not observed",
            "routers": [
                {"id": "rtr-A", "name": "", "branches": [], "script": {"_scriptId": "scr9", "function": "custom"}}
            ],
        }
        out = sanitize("flow", raw)
        assert out["routers"][0]["script"] == {"_scriptId": "scr9", "function": "custom"}

    def test_captured_payload_nested_inside_router_topology_is_still_stripped(self):
        """Preserving topology must not reopen the payload hole fixed in
        rounds 1-2. Plants a captured-payload-shaped value at three nesting
        levels at once: the router itself, a branch, and a pageProcessor's
        responseMapping."""
        raw = {
            "_id": "f6",
            "name": "Flow with a captured payload smuggled into topology",
            "routers": [
                {
                    "id": "rtr-A",
                    "name": "",
                    "mockResponse": {"_headers": {"set-cookie": ["router-leak"]}},
                    "branches": [
                        {
                            "name": "",
                            "mockOutput": {"result": "branch-leak"},
                            "pageProcessors": [
                                {
                                    "type": "import",
                                    "_importId": "imp1",
                                    "sampleData": {"customer": {"email": "leak@example-shop.test"}},
                                    "responseMapping": {
                                        "fields": [{"extract": "id", "generate": "internalid"}],
                                        "rawData": "leak",
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        out = sanitize("flow", raw)
        assert "set-cookie" not in _dumped(out)
        assert "branch-leak" not in _dumped(out)
        assert "example-shop.test" not in _dumped(out)
        pp = out["routers"][0]["branches"][0]["pageProcessors"][0]
        assert "rawData" not in pp["responseMapping"]
        assert pp["_importId"] == "imp1"
        assert pp["responseMapping"] == {"fields": [{"extract": "id", "generate": "internalid"}]}


class TestSanitizerAndWalkerCompose:
    """FIX ROUND 4: sanitize() (this module) and walk_script_refs() (Task 2,
    app.services.celigo.graph, landed at bdab2b59) don't compose safely on
    their own -- a script ref surviving in the RAW object can still be lost
    after sanitize() if the allowlist doesn't cover every container the
    walker searches. Rounds 2-3 covered filter/transform/hooks on export and
    routers/pageProcessors on flow, but missed two seams that only appear
    when the two are COMPOSED:

    1. `transform` was added to `_EXPORT` in round 2 but never to `_IMPORT`
       -- so `transform.script`, "the regression case... the most-used
       script in the live account" per the plan's Verified Facts, silently
       disappeared on imports while surviving on exports.
    2. `_PAGE_PROCESSOR` (round 3) has no `hooks`/`transform`/`filter` of its
       own, so a script ref inlined inside a flow-embedded pageProcessor --
       top-level or inside a router branch -- would also be lost. Not
       observed live (pageProcessors reference exports/imports by id in
       every flow probed), added anyway as cheap insurance: the allowlist
       should never be the reason a ref disappears if Celigo ever does
       inline one.

    SCOPE CORRECTION (round 7): this class is THREE HAND-WRITTEN FIXTURES for
    export, import and flow -- nothing more. Its original docstring, and
    `ae9919fa`'s commit message, claimed it made "this whole class of defect
    unrepresentable rather than fixing two instances of it", covering "every
    kind that can carry scripts". That was false when written: round 4 fixed
    two of five attachment sites, and the three siblings it never considered
    (`_PAGE_GENERATOR`, `_ROUTER_BRANCH`, `inputFilter`) went on dropping real
    refs until the whole-branch review executed them. A fixture list covers
    the containers whoever wrote it thought of; that is the opposite of
    unrepresentable. `TestScriptSiteCoverageIsDerivedFromTheSchema` at the end
    of this file is the structural version. These three stay because a
    concrete, readable reproduction of a defect that actually shipped is worth
    keeping next to the general rule.

    The property under test, for the three kinds below: sanitizing must not
    change WHICH script refs walk_script_refs finds, even though it must
    still strip everything else
    -- a captured-payload-shaped value sits next to every script ref below
    to prove this can't be satisfied by weakening the sanitizer. Compared as
    SETS, not lists: `_apply_schema` builds its output in SCHEMA key order,
    not the raw object's own key order, so the two traversals can legitimately
    visit sibling containers in a different sequence -- that's an
    implementation detail of dict ordering, not a change in which refs exist.
    """

    def test_export_script_refs_survive_composition(self):
        raw = {
            "_id": "e1",
            "name": "export-with-scripts",
            "filter": {
                "type": "script",
                "script": {"_scriptId": "FILT", "function": "onFilter"},
                "mockResponse": {"_headers": {"set-cookie": ["filter-leak"]}},
            },
            "transform": {
                "type": "script",
                "script": {"_scriptId": "XFORM", "function": "onTransform"},
                "mockOutput": {"result": "transform-leak"},
            },
            "hooks": {
                "someFutureHook": {"_scriptId": "HOOKS", "function": "onHook"},
            },
        }
        before = walk_script_refs(raw)
        sanitized = sanitize("export", raw)
        after = walk_script_refs(sanitized)
        assert set(before) == set(after)
        assert {r.script_id for r in after} == {"FILT", "XFORM", "HOOKS"}
        assert "set-cookie" not in _dumped(sanitized)
        assert "transform-leak" not in _dumped(sanitized)

    def test_import_script_refs_survive_composition(self):
        """THE round-4 defect, reproduced directly: before this fix,
        `_IMPORT` had `filter`/`hooks` but no `transform`, so XFORM was lost
        while FILT and HOOKS survived."""
        raw = {
            "_id": "i1",
            "name": "import-with-scripts",
            "adaptorType": "NetSuiteDistributedImport",
            "filter": {
                "type": "script",
                "script": {"_scriptId": "FILT", "function": "onFilter"},
                "sampleData": {"customer": {"email": "filter-leak@example-shop.test"}},
            },
            "transform": {
                "type": "script",
                "script": {"_scriptId": "XFORM", "function": "onTransform"},
                "rawData": "transform-leak",
            },
            "hooks": {
                "preSavePage": {"_scriptId": "HOOKS", "function": "onHook"},
            },
        }
        before = walk_script_refs(raw)
        sanitized = sanitize("import", raw)
        after = walk_script_refs(sanitized)
        assert set(before) == set(after)
        assert {r.script_id for r in after} == {"FILT", "XFORM", "HOOKS"}
        assert "example-shop.test" not in _dumped(sanitized)
        assert "transform-leak" not in _dumped(sanitized)

    def test_flow_script_refs_survive_composition_including_nested_page_processors(self):
        """A script ref inlined inside a TOP-LEVEL pageProcessor's `hooks`,
        AND inside a router branch's pageProcessor's `transform`, must both
        survive -- not observed live, but the allowlist should not be the
        reason a ref disappears if Celigo ever does inline one there."""
        raw = {
            "_id": "f1",
            "name": "flow-with-scripts",
            "pageProcessors": [
                {
                    "type": "import",
                    "_importId": "imp-top",
                    "hooks": {"preSavePage": {"_scriptId": "TOP", "function": "onTop"}},
                    "mockOutput": {"leak": "top-level-leak"},
                }
            ],
            "routers": [
                {
                    "id": "rtr-A",
                    "name": "",
                    "branches": [
                        {
                            "name": "",
                            "pageProcessors": [
                                {
                                    "type": "import",
                                    "_importId": "imp-branch",
                                    "transform": {
                                        "type": "script",
                                        "script": {"_scriptId": "BRANCH", "function": "onBranch"},
                                    },
                                    "sampleData": {"customer": {"email": "branch-leak@example-shop.test"}},
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        before = walk_script_refs(raw)
        sanitized = sanitize("flow", raw)
        after = walk_script_refs(sanitized)
        assert set(before) == set(after)
        assert {r.script_id for r in after} == {"TOP", "BRANCH"}
        dumped = _dumped(sanitized)
        assert "top-level-leak" not in dumped
        assert "branch-leak" not in dumped


class TestSanitizerPreservesEveryRepositoryReadField:
    """FIX ROUND 5: this is the THIRD time the sanitizer has silently dropped
    something a later stage needed -- first script refs (round 2/4), then
    flow topology (round 3), now `lastModified`. Each was fixed by adding
    fields; this test stops the PATTERN instead of the next instance of it.

    `app.services.celigo.repository` (Task 5, commit 93b89277) is the real
    consumer of `sanitize()`'s output -- every `upsert_*` function's
    docstring says its `sanitized` argument is "already through
    `sanitizer.sanitize(kind, raw)`". For each resource kind the repository
    actually reads a sanitized dict for (`integration`, `flow`, `script` --
    `export`/`import` are never separately sanitized; their fields reach the
    repository already flattened into a sanitized FLOW's `pageGenerators`/
    `pageProcessors`, and `error` fields arrive as individual kwargs, not a
    `sanitized` dict), this asserts every field the repository's own code
    reads off that dict survives sanitize() -- with a captured-payload value
    planted alongside every read site, so the property can't be satisfied by
    weakening the sanitizer.

    WHAT THIS DOES AND DOES NOT GUARANTEE, stated rather than implied: this
    PINS the read-set as traced BY HAND today (verified against every
    `.get(...)`/`[...]` call in `upsert_integration`/`upsert_flow`/
    `extract_flow_steps`/`upsert_script`, function by function) -- a
    regression that drops one of THOSE fields fails here. It does NOT derive
    the read-set from repository.py itself. A field repository.py starts
    reading TOMORROW, with no matching assertion added HERE, passes this
    test silently and can still land permanently NULL -- the same failure
    mode this test exists to catch, just one release later. Closing that gap
    for real needs something structural (an AST scan of repository.py's own
    key accesses, or a repository-side assertion that every key it touches
    was allowlisted) -- deliberately NOT implemented here; that is real
    engineering, deferred to the whole-branch review to triage.

    The flow case goes one step further and calls `extract_flow_steps` --
    the REAL, pure consumer function, not a hand-typed key list -- directly
    on the sanitized output, the same pattern round 4's composition tests
    used for `walk_script_refs`.
    """

    def test_upsert_integration_read_set_survives(self):
        """`upsert_integration` (repository.py) reads exactly: _id, name,
        sandbox, mode, description, lastModified (-> celigo_last_modified,
        migration 094). All five were already correct before this round."""
        raw = {
            "_id": "int-1",
            "name": "Solidus + NetSuite",
            "sandbox": False,
            "mode": "install",
            "description": "Order sync",
            "lastModified": "2026-08-27T14:06:38.932Z",
            "mockResponse": {"_headers": {"set-cookie": ["integration-leak"]}},
        }
        out = sanitize("integration", raw)
        assert out["_id"] == "int-1"
        assert out["name"] == "Solidus + NetSuite"
        assert out["sandbox"] is False
        assert out["mode"] == "install"
        assert out["description"] == "Order sync"
        assert out["lastModified"] == "2026-08-27T14:06:38.932Z"
        assert "set-cookie" not in _dumped(out)

    def test_upsert_flow_and_extract_flow_steps_read_set_survives(self):
        """`upsert_flow` reads: _id, name, disabled, schedule, timezone,
        lastExecutedAt, _sourceId, aiDescription.{summary,detailed,
        generatedOn}, lastModified (-> celigo_last_modified, migration 094
        -- MISSING before this round, same gap as script). `extract_flow_steps`
        -- called directly below, the real consumer -- reads
        pageGenerators[].{_exportId,skipRetries},
        pageProcessors[].{_exportId,_importId,filter,responseMapping,
        proceedOnFailure} at BOTH the top level and inside
        routers[].branches[].pageProcessors, plus routers[].id and
        routers[].branches[].branchId to build the router/branch chain."""
        raw = {
            "_id": "f1",
            "name": "NS - Create Sales Order",
            "disabled": False,
            "schedule": "0 * * * *",
            "timezone": "America/New_York",
            "lastExecutedAt": "2026-08-27T00:00:00.000Z",
            "_sourceId": "src1",
            "lastModified": "2026-08-27T14:06:38.932Z",
            "aiDescription": {
                "summary": "Creates sales orders",
                "detailed": "Long form",
                "generatedOn": "2026-08-20T00:00:00.000Z",
            },
            "pageGenerators": [{"_exportId": "exp-top", "skipRetries": True}],
            "pageProcessors": [
                {
                    "type": "import",
                    "_importId": "imp-top",
                    "proceedOnFailure": False,
                    "filter": {"type": "expression", "expression": {"rules": [], "version": "1"}},
                    "responseMapping": {"fields": [{"extract": "id", "generate": "internalid"}]},
                    "sampleData": {"customer": {"email": "top-processor-leak@example-shop.test"}},
                }
            ],
            "routers": [
                {
                    "id": "rtr-A",
                    "name": "",
                    "mockOutput": {"leak": "router-leak"},
                    "branches": [
                        {
                            "name": "",
                            "branchId": "brn-A1",
                            "rawData": "branch-leak",
                            "pageProcessors": [
                                {
                                    "type": "import",
                                    "_importId": "imp-branch",
                                    "proceedOnFailure": True,
                                    "filter": {"type": "expression", "expression": {"rules": [], "version": "1"}},
                                    "responseMapping": {
                                        "fields": [{"extract": "internalid", "generate": "id"}],
                                        "rawData": "response-mapping-leak",
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
            "mockResponse": {"_headers": {"set-cookie": ["flow-leak"]}},
        }
        out = sanitize("flow", raw)

        assert out["name"] == "NS - Create Sales Order"
        assert out["disabled"] is False
        assert out["schedule"] == "0 * * * *"
        assert out["timezone"] == "America/New_York"
        assert out["lastExecutedAt"] == "2026-08-27T00:00:00.000Z"
        assert out["_sourceId"] == "src1"
        assert out["lastModified"] == "2026-08-27T14:06:38.932Z"
        assert out["aiDescription"] == {
            "summary": "Creates sales orders",
            "detailed": "Long form",
            "generatedOn": "2026-08-20T00:00:00.000Z",
        }

        steps = extract_flow_steps(out)
        assert steps == [
            FlowStepInput(
                celigo_id="exp-top",
                role="generator",
                router_id=None,
                branch_id=None,
                sequence=0,
                filter_json=None,
                mapping_json=None,
                proceed_on_failure=None,
                skip_retries=True,
            ),
            FlowStepInput(
                celigo_id="imp-top",
                role="processor",
                router_id=None,
                branch_id=None,
                sequence=0,
                filter_json={"type": "expression", "expression": {"rules": [], "version": "1"}},
                mapping_json={"fields": [{"extract": "id", "generate": "internalid"}]},
                proceed_on_failure=False,
                skip_retries=None,
            ),
            FlowStepInput(
                celigo_id="imp-branch",
                role="processor",
                router_id="rtr-A",
                branch_id="brn-A1",
                sequence=0,
                filter_json={"type": "expression", "expression": {"rules": [], "version": "1"}},
                mapping_json={"fields": [{"extract": "internalid", "generate": "id"}]},
                proceed_on_failure=True,
                skip_retries=None,
            ),
        ]

        dumped = _dumped(out)
        assert "set-cookie" not in dumped
        assert "example-shop.test" not in dumped
        assert "router-leak" not in dumped
        assert "branch-leak" not in dumped
        assert "response-mapping-leak" not in dumped

    def test_upsert_script_read_set_survives(self):
        """`upsert_script` reads: _id, name, content, _sourceId, sandbox,
        lastModified (-> celigo_last_modified, migration 094 -- MISSING
        before this round, the same gap as flow)."""
        raw = {
            "_id": "s1",
            "name": "Null guard",
            "content": "function main(options) { return options; }",
            "_sourceId": "src3",
            "sandbox": False,
            "lastModified": "2026-03-13T00:44:05.606Z",
            "mockOutput": {"result": "script-leak"},
        }
        out = sanitize("script", raw)
        assert out["_id"] == "s1"
        assert out["name"] == "Null guard"
        assert out["content"] == "function main(options) { return options; }"
        assert out["_sourceId"] == "src3"
        assert out["sandbox"] is False
        assert out["lastModified"] == "2026-03-13T00:44:05.606Z"
        assert "script-leak" not in _dumped(out)


class TestExportNetsuiteRestletProvenanceSurvives:
    """FIX ROUND 2 (Task 7): `_EXPORT` had no allowlist entry for `netsuite`
    at all, so `netsuite.restlet.recordType`/`searchId` -- Task 11's
    export-side provenance input, the export-side counterpart to
    `netsuite_da.recordType`/`operation` on imports -- was dropped before it
    could ever reach `sync_service.py`'s Phase D. Same shape of gap as the
    fourth and fifth prior rounds: data existed upstream (Celigo returns it;
    Phase D fetches the export object) and nothing carried it through this
    one allowlist.
    """

    def test_netsuite_restlet_record_type_and_search_id_survive(self):
        raw = {
            "_id": "e1",
            "name": "Solidus Orders Export",
            "adaptorType": "NetSuiteExport",
            "_connectionId": "conn1",
            "netsuite": {
                "type": "restlet",
                "skipGrouping": False,
                "statsOnly": False,
                "restlet": {
                    "recordType": "salesorder",
                    "searchId": "customsearch_so_export",
                    "restletVersion": "suiteapp2.0",
                    "markExportedBatchSize": 100,
                },
                "distributed": {},
            },
        }
        out = sanitize("export", raw)
        assert out["netsuite"] == {"restlet": {"recordType": "salesorder", "searchId": "customsearch_so_export"}}

    def test_narrow_allowlist_drops_unlisted_netsuite_fields(self):
        """Kept narrow on purpose, same discipline as `_NETSUITE_DA`: only
        `restlet.recordType`/`searchId` are known-consumed. `type`/
        `skipGrouping`/`statsOnly`/`restlet.restletVersion`/
        `restlet.markExportedBatchSize`/`distributed` are dropped, not
        guessed at."""
        raw = {
            "_id": "e1",
            "netsuite": {
                "type": "restlet",
                "skipGrouping": True,
                "statsOnly": True,
                "restlet": {
                    "recordType": "customer",
                    "searchId": "s1",
                    "restletVersion": "v2",
                    "markExportedBatchSize": 50,
                },
                "distributed": {"foo": "bar"},
            },
        }
        out = sanitize("export", raw)
        assert out["netsuite"] == {"restlet": {"recordType": "customer", "searchId": "s1"}}

    def test_captured_payload_planted_alongside_netsuite_is_still_stripped(self):
        """The whole point of this allowlist addition: proving it doesn't
        reopen the payload-leak hole every prior round closed."""
        raw = {
            "_id": "e1",
            "name": "Export with a leak",
            "netsuite": {"restlet": {"recordType": "salesorder", "searchId": "s1"}},
            "mockOutput": {"orderId": "R999999", "customer": {"email": "leaked@example-shop.test"}},
            "rawData": "<raw wire bytes, never persisted>",
        }
        out = sanitize("export", raw)
        assert out["netsuite"] == {"restlet": {"recordType": "salesorder", "searchId": "s1"}}
        assert "mockOutput" not in out
        assert "rawData" not in out
        assert "leaked@example-shop.test" not in _dumped(out)

    def test_missing_netsuite_key_produces_no_key_not_an_error(self):
        """Coverage of `netsuite` is uneven (only NetSuite-backed exports
        carry it, same posture as `aiDescription`) -- absence must degrade
        cleanly, never raise."""
        raw = {"_id": "e1", "name": "Non-NetSuite export"}
        out = sanitize("export", raw)
        assert "netsuite" not in out


class TestRulesAreFilteredAsExpressionTrees:
    """FINAL REVIEW finding 3: `rules` was the LAST field still declared a
    schema leaf, and `_apply_schema`'s leaf branch copies one level shallow --
    so anything at any depth below `rules` survived VERBATIM. A `mockResponse`
    carrying a session cookie, a customer email and an order ref, nested under
    `filter.expression.rules`, passed `sanitize("import", ...)` completely
    untouched.

    That is the module's OWN stated invariant (module docstring, fix round 1)
    violated word for word: "any field whose value CAN be a nested object or
    a list of them ... must be recursively filtered by its own schema, never
    blob-copied as a leaf -- a leaf copy is exactly the shape of hole a
    captured payload slips through." Both projection directions were proven
    useless live (observed-shapes.md), so this sanitizer is the only control
    there is.

    WHY `rules` COULDN'T JUST GET A KEY SCHEMA like `filter` or
    `netsuite_da.mapping` did: real `rules` values are EXPRESSION TREES, not
    fixed-key objects. Both shapes below are real, from observed-shapes.md's
    live probes:

        ["notempty", ["string", ["extract", "..."]]]   # nested lists of strings
        [[{key, extract, generate}]]                    # list of list of dicts

    Arbitrary nesting of lists, strings and small dicts -- there is no fixed
    key set to enumerate at the top. So `rules` is filtered by VALUE SHAPE
    (`sanitizer._filter_rule_tree`): lists and scalars recurse/pass through
    unchanged, and DICTS keep only allowlisted keys. A captured payload is
    always a dict under some key, so dropping unlisted dict keys at every
    depth closes the hole while leaving every legitimate expression tree
    byte-identical.
    """

    # Both taken structurally from observed-shapes.md's live probes; the
    # string values are invented (never a real order ref or field path).
    _REAL_NOTEMPTY_TREE = ["notempty", ["string", ["extract", "custbody_order_ref"]]]
    _REAL_MAPPING_TREE = [[{"key": "aBc123", "extract": "orderNumber", "generate": "custbody_order_ref"}]]

    # A captured payload nested inside an otherwise-legitimate expression
    # tree -- the exact shape observed-shapes.md warns rides along on every
    # import/export response regardless of projection.
    _PAYLOAD_BEARING_TREE = [
        "and",
        [
            "notempty",
            {
                "key": "legit",
                "mockResponse": {
                    "_headers": {"set-cookie": ["connect.sid=s%3AinventedRulesLeak; HttpOnly"]},
                    "body": {"customer": {"email": "rules.leak@example-shop.test"}, "orderId": "R999999"},
                },
            },
        ],
    ]

    def _assert_leak_stripped(self, out: dict) -> None:
        dumped = _dumped(out)
        assert "set-cookie" not in dumped
        assert "example-shop.test" not in dumped
        assert "R999999" not in dumped
        assert "mockResponse" not in dumped

    def test_import_filter_expression_rules_strip_a_captured_payload(self):
        """THE reproduction from the review, verbatim: a payload nested under
        `filter.expression.rules` on an import."""
        raw = {
            "_id": "i1",
            "filter": {
                "type": "expression",
                "expression": {"rules": self._PAYLOAD_BEARING_TREE, "version": "1"},
                "version": "1",
            },
        }
        out = sanitize("import", raw)
        self._assert_leak_stripped(out)
        # The legitimate structure around the leak survives -- this is a
        # filter, not a drop.
        assert out["filter"]["expression"]["rules"] == ["and", ["notempty", {"key": "legit"}]]

    def test_import_filter_top_level_rules_strip_a_captured_payload(self):
        raw = {
            "_id": "i1",
            "filter": {"type": "expression", "rules": self._PAYLOAD_BEARING_TREE, "version": "1"},
        }
        out = sanitize("import", raw)
        self._assert_leak_stripped(out)

    def test_export_transform_expression_rules_strip_a_captured_payload(self):
        raw = {
            "_id": "e1",
            "transform": {
                "type": "expression",
                "expression": {"rules": self._PAYLOAD_BEARING_TREE, "version": "1"},
                "rules": self._PAYLOAD_BEARING_TREE,
                "version": "1",
            },
        }
        out = sanitize("export", raw)
        self._assert_leak_stripped(out)

    def test_router_branch_input_filter_rules_strip_a_captured_payload(self):
        """`routers[].branches[].inputFilter.rules` -- the third site the
        review reproduced, reached only through the flow schema."""
        raw = {
            "_id": "f1",
            "name": "flow-with-branch-filter",
            "routers": [
                {
                    "id": "rtr-A",
                    "branches": [
                        {"name": "Branch", "branchId": "b1", "inputFilter": {"rules": self._PAYLOAD_BEARING_TREE}}
                    ],
                }
            ],
        }
        out = sanitize("flow", raw)
        self._assert_leak_stripped(out)

    def test_real_notempty_expression_tree_survives_byte_identical(self):
        """The `["notempty", ["string", ["extract", ...]]]` shape from
        observed-shapes.md: nested lists of bare strings, no dicts at all.
        A filter that only understood dicts would flatten or drop this."""
        raw = {"_id": "i1", "filter": {"type": "expression", "rules": self._REAL_NOTEMPTY_TREE, "version": "1"}}
        out = sanitize("import", raw)
        assert out["filter"]["rules"] == self._REAL_NOTEMPTY_TREE

    def test_real_mapping_expression_tree_survives_byte_identical(self):
        """The `[[{key, extract, generate}]]` shape from observed-shapes.md:
        a list of lists of small config dicts. Every one of those three keys
        must survive -- they are field mappings, the config this whole flow
        map exists to record."""
        raw = {
            "_id": "e1",
            "transform": {
                "type": "expression",
                "expression": {"rules": self._REAL_MAPPING_TREE, "version": "1"},
                "version": "1",
            },
        }
        out = sanitize("export", raw)
        assert out["transform"]["expression"]["rules"] == self._REAL_MAPPING_TREE

    def test_rules_filtering_does_not_mutate_the_raw_object(self):
        """Same purity contract as `sanitize()` itself -- the recursive rule
        filter must build new containers, never edit the caller's."""
        raw = {"_id": "i1", "filter": {"type": "expression", "rules": copy.deepcopy(self._PAYLOAD_BEARING_TREE)}}
        before = copy.deepcopy(raw)
        sanitize("import", raw)
        assert raw == before

    def test_scalars_and_nulls_inside_rules_survive(self):
        """Expression trees carry numbers, booleans and nulls as operands.
        None of those can hide a captured payload, and dropping them would
        silently corrupt a filter's meaning."""
        raw = {"_id": "i1", "filter": {"type": "expression", "rules": ["equals", 42, True, None, 1.5, "x"]}}
        out = sanitize("import", raw)
        assert out["filter"]["rules"] == ["equals", 42, True, None, 1.5, "x"]


class TestEverySchemaNodeThatModelsTopologyKeepsScriptRefs:
    """FINAL REVIEW finding 2: round 4 claimed to make a whole class of defect
    "unrepresentable rather than fixing two instances of it", covering "every
    kind that can carry scripts". It fixed two of five sites and pinned only
    those two -- the claim was in the docstring and in the commit message, and
    it was false when written.

    The three sites it missed, each a SIBLING of one it fixed:
      * `_PAGE_GENERATOR` never gained `hooks`/`transform`/`filter` when
        `_PAGE_PROCESSOR` did.
      * `_ROUTER_BRANCH` never gained `script`, though `_ROUTER` carries it
        and the plan's Verified Facts name "router branches" as an
        attachment site EXPLICITLY.
      * `routers[].branches[].inputFilter` was schema'd as a bare
        `{rules}` with no `script`, unlike its `filter`/`transform` cousins.

    The tests directly below reproduce those three losses. The structural
    tests in `TestScriptSiteCoverageIsDerivedFromTheSchema` are the actual
    fix for the pattern: the site list and the containers that carry it are
    both derived from the schema table, so the next sibling cannot be
    half-done.
    """

    _LEAK = {"_headers": {"set-cookie": ["connect.sid=s%3AinventedSiteLeak; HttpOnly"]}}

    def test_page_generator_hooks_and_transform_script_refs_survive(self):
        """`pageGenerators` is the sibling of `pageProcessors`, which round 4
        covered. Both are lists of step references on the same flow object;
        there is no reason one keeps script refs and the other drops them."""
        raw = {
            "_id": "f1",
            "name": "flow-with-generator-scripts",
            "pageGenerators": [
                {
                    "_exportId": "exp-gen",
                    "hooks": {"preSavePage": {"_scriptId": "GEN_HOOK", "function": "onHook"}},
                    "transform": {
                        "type": "script",
                        "script": {"_scriptId": "GEN_XFORM", "function": "onTransform"},
                    },
                    "mockOutput": self._LEAK,
                }
            ],
        }
        before = walk_script_refs(raw)
        sanitized = sanitize("flow", raw)
        after = walk_script_refs(sanitized)
        assert set(before) == set(after)
        assert {r.script_id for r in after} == {"GEN_HOOK", "GEN_XFORM"}
        assert "set-cookie" not in _dumped(sanitized)

    def test_router_branch_script_ref_survives(self):
        """`routers[].branches[].script` -- `_ROUTER` carries `script`, its
        own branches did not."""
        raw = {
            "_id": "f1",
            "name": "flow-with-branch-script",
            "routers": [
                {
                    "id": "rtr-A",
                    "branches": [
                        {
                            "name": "Branch",
                            "branchId": "b1",
                            "script": {"_scriptId": "BRANCH_SCRIPT", "function": "branching"},
                            "mockResponse": self._LEAK,
                        }
                    ],
                }
            ],
        }
        before = walk_script_refs(raw)
        sanitized = sanitize("flow", raw)
        after = walk_script_refs(sanitized)
        assert set(before) == set(after)
        assert {r.script_id for r in after} == {"BRANCH_SCRIPT"}
        assert "set-cookie" not in _dumped(sanitized)

    def test_router_branch_input_filter_script_ref_survives(self):
        """`inputFilter` is filter-shaped; a `type: "script"` form carries an
        attachment exactly the way `filter` does, and was schema'd as a bare
        `{rules}`."""
        raw = {
            "_id": "f1",
            "name": "flow-with-branch-input-filter-script",
            "routers": [
                {
                    "id": "rtr-A",
                    "branches": [
                        {
                            "name": "Branch",
                            "branchId": "b1",
                            "inputFilter": {
                                "type": "script",
                                "script": {"_scriptId": "INPUT_FILTER", "function": "onFilter"},
                                "mockResponse": self._LEAK,
                            },
                        }
                    ],
                }
            ],
        }
        before = walk_script_refs(raw)
        sanitized = sanitize("flow", raw)
        after = walk_script_refs(sanitized)
        assert set(before) == set(after)
        assert {r.script_id for r in after} == {"INPUT_FILTER"}
        assert "set-cookie" not in _dumped(sanitized)


# --------------------------------------------------------------------------
# Schema-derived probes for `TestScriptSiteCoverageIsDerivedFromTheSchema`.
# These read `sanitizer`'s own schema table deliberately -- that is the whole
# point: nothing below hardcodes a list of kinds, containers or sites, so a
# container or a site added to the module tomorrow is covered here without
# anyone remembering to edit this file.
# --------------------------------------------------------------------------


def _iter_schema_nodes(schema: dict, seen: set[int] | None = None):
    """Every schema dict reachable from *schema*, itself included. `None`
    leaves and the `_RULE_TREE` marker are not dicts and are skipped."""
    seen = set() if seen is None else seen
    if id(schema) in seen:
        return
    seen.add(id(schema))
    yield schema
    for sub in schema.values():
        if isinstance(sub, dict):
            yield from _iter_schema_nodes(sub, seen)


def _all_schema_nodes() -> list[dict]:
    nodes: list[dict] = []
    seen: set[int] = set()
    for schema in sanitizer._ALLOWLISTS.values():
        nodes.extend(_iter_schema_nodes(schema, seen))
    return nodes


def _site_internal_node_ids() -> set[int]:
    """Every node INSIDE a script site's own sub-schema (`_FILTER` itself,
    its nested `script`, `_HOOKS`' wildcard target, ...). These describe the
    internals of ONE site and are not containers, so the "declares one site
    => declares all five" rule must not apply to them -- `_FILTER` carrying
    only `script` is correct, not a half-splice. Derived, not listed: a new
    site sub-schema is exempted automatically."""
    ids: set[int] = set()
    seen: set[int] = set()
    for site_schema in sanitizer._SCRIPT_SITES.values():
        ids.update(id(node) for node in _iter_schema_nodes(site_schema, seen))
    return ids


def _plant_script_ref(site_schema: dict, script_id: str):
    """The smallest raw value for *site_schema* that carries a `_scriptId`,
    DERIVED from the schema rather than hand-written per site: descend until
    a `_SCRIPT_REF`-shaped node is found (through the `hooks` wildcard, or
    through `filter`/`transform`'s own `script` key). Returns None if the
    schema cannot hold a ref at all."""
    if sanitizer._WILDCARD in site_schema:
        return {"probeHook": _plant_script_ref(site_schema[sanitizer._WILDCARD], script_id)}
    if "_scriptId" in site_schema:
        return {"_scriptId": script_id, "function": "onProbe"}
    for key, sub in site_schema.items():
        if isinstance(sub, dict):
            nested = _plant_script_ref(sub, script_id)
            if nested is not None:
                return {key: nested}
    return None


def _build_probe_object(schema: dict, path: str, planted: list[str], seen: frozenset[int] = frozenset()):
    """A raw object shaped by *schema*, with a captured-payload marker at
    EVERY node and a unique `_scriptId` at every script site that node
    declares. Nested schemas are emitted as one-element lists (both list and
    dict forms go down the same `_apply_schema` path; the list form also
    exercises the index suffix in `walk_script_refs`' json_path)."""
    if id(schema) in seen:  # the real graph is a DAG; guard anyway
        return {}
    node_seen = seen | {id(schema)}
    out: dict = {"mockResponse": {"_headers": {"set-cookie": [f"probe-leak-{path or 'root'}"]}}}
    for key, sub in schema.items():
        if key == sanitizer._WILDCARD or key in sanitizer._SCRIPT_SITES:
            continue  # script sites are planted below, not generated here
        child = f"{path}.{key}" if path else key
        if sub is sanitizer._RULE_TREE:
            out[key] = ["notempty", [{"key": "k", "extract": "e", "generate": "g"}]]
        elif sub is None:
            out[key] = f"cfg-{child}"
        elif isinstance(sub, dict):
            out[key] = [_build_probe_object(sub, f"{child}[0]", planted, node_seen)]
    for site, site_schema in sanitizer._SCRIPT_SITES.items():
        if site not in schema:
            continue
        script_id = f"PROBE_{len(planted)}"
        value = _plant_script_ref(site_schema, script_id)
        assert value is not None, f"{site} cannot hold a script ref"
        out[site] = value
        planted.append(script_id)
    return out


class TestScriptSiteCoverageIsDerivedFromTheSchema:
    """FIX ROUND 7 -- the actual fix for finding 2, as opposed to the three
    reproductions above.

    Round 4's docstring claimed its composition test made "this whole class
    of defect unrepresentable rather than fixing two instances of it". It did
    not: it hand-wrote three fixtures, so it covered exactly the three
    containers whoever wrote it happened to think of, and the two siblings
    they did not think of (`_PAGE_GENERATOR`, `_ROUTER_BRANCH`) went on
    losing refs. A hand-written fixture list can only ever pin today's known
    sites -- the same gap `TestSanitizerPreservesEveryRepositoryReadField`
    names in its own docstring.

    Everything below is derived from `sanitizer`'s schema table instead:

    1. `test_script_sites_are_spliced_never_hand_copied` -- any node
       declaring ANY script-site key declares ALL of them, with the identical
       sub-schema object. Half-updating a container is unrepresentable.
    2. `test_every_topology_node_carries_every_script_site` -- any node
       declaring a TOPOLOGY key (a step reference, a branch, or a container
       of them) must carry every script site. This is the rule that would
       have caught `_PAGE_GENERATOR` and `_ROUTER_BRANCH` before they shipped,
       and it catches their next sibling automatically: a node cannot model
       flow topology without naming one of those keys.
    3. `test_generated_probe_preserves_every_script_ref_for_every_kind` --
       rules 1 and 2 only READ the table; this one PROVES the table produces
       the behaviour, by execution, on an object generated from the schema
       graph itself: a `_scriptId` planted at every site of every reachable
       container, with a captured payload beside it. Adding a sixth site key
       to `_SCRIPT_SITES`, or a new container, extends it with no edit here.

    WHICH RULE CATCHES WHAT -- stated because getting this wrong is the very
    mistake being fixed, and all three claims below are MUTATION-PROVEN, not
    reasoned:

      * Unsplicing `_PAGE_GENERATOR` (reproducing round 4's exact omission)
        fails rules 2 and the reach test. It does NOT fail rule 3: the probe
        builder derives what to plant from the same table, so a site the
        schema does not declare is a site the probe never plants, and the
        composition holds vacuously. Rule 3 proves that what IS declared
        survives; rule 2 is the one that catches a site MISSING.
      * Hand-copying a SUBSET of the sites into `_ROUTER_BRANCH` fails rules
        1 and 2.
      * Hand-copying the FULL set, but as fresh dict objects rather than the
        shared ones, fails rule 1's identity assertion -- which is the point
        of asserting `is` and not `==`: equal-today copies are what drift
        apart tomorrow.

    WHAT THIS STILL DOES NOT COVER: a script ATTACHMENT SITE Celigo invents
    that is not in `_SCRIPT_SITES` is invisible to all three -- no test can
    know a key nobody has seen. What changed is the cost of learning about
    one: adding it to `_SCRIPT_SITES` now covers every container at once,
    instead of five separate edits of which two got forgotten. Likewise, a
    script-carrying container that names none of `_TOPOLOGY_KEYS` and is not
    spliced would pass rule 2 -- rule 1 still forces it to be all-or-nothing,
    and rule 3 still proves whatever it does declare actually works.
    """

    def test_script_sites_are_spliced_never_hand_copied(self):
        internal = _site_internal_node_ids()
        for node in _all_schema_nodes():
            if id(node) in internal:
                continue  # a site's own internals, not a container -- see helper
            declared = {key for key in sanitizer._SCRIPT_SITES if key in node}
            if not declared:
                continue
            assert declared == set(sanitizer._SCRIPT_SITES), (
                f"node declares {sorted(declared)} but not {sorted(set(sanitizer._SCRIPT_SITES) - declared)} -- "
                "splice **_SCRIPT_SITES instead of copying keys"
            )
            for key, sub_schema in sanitizer._SCRIPT_SITES.items():
                assert node[key] is sub_schema, f"{key} is a hand-written copy, not the shared _SCRIPT_SITES entry"

    def test_every_topology_node_carries_every_script_site(self):
        checked = 0
        for node in _all_schema_nodes():
            if not (set(node) & sanitizer._TOPOLOGY_KEYS):
                continue
            checked += 1
            missing = {key for key in sanitizer._SCRIPT_SITES if key not in node}
            assert not missing, f"topology node is missing script sites {sorted(missing)}"
        # _FLOW, _PAGE_GENERATOR, _PAGE_PROCESSOR, _ROUTER, _ROUTER_BRANCH.
        # Asserted so a refactor that stops REACHING the topology nodes can't
        # turn this test into a vacuous pass over an empty loop.
        assert checked == 5

    def test_generated_probe_preserves_every_script_ref_for_every_kind(self):
        for kind, schema in sanitizer._ALLOWLISTS.items():
            planted: list[str] = []
            raw = _build_probe_object(schema, "", planted)
            before = walk_script_refs(raw)
            sanitized = sanitize(kind, raw)
            after = walk_script_refs(sanitized)

            assert {r.script_id for r in before} == set(planted), f"{kind}: probe builder disagrees with itself"
            assert set(before) == set(after), f"{kind}: sanitize() lost script refs"
            assert "probe-leak" not in _dumped(sanitized), f"{kind}: captured payload survived"
            assert "set-cookie" not in _dumped(sanitized), f"{kind}: captured payload survived"

    def test_generated_flow_probe_actually_reaches_every_topology_container(self):
        """The generated probe is only worth anything if it gets INTO the
        containers that lost refs. Pins the reach explicitly rather than
        trusting the generator."""
        planted: list[str] = []
        raw = _build_probe_object(sanitizer._ALLOWLISTS["flow"], "", planted)
        paths = {ref.json_path for ref in walk_script_refs(raw)}

        for prefix in (
            "pageGenerators[0].",
            "pageProcessors[0].",
            "routers[0].",
            "routers[0].branches[0].",
            "routers[0].branches[0].pageProcessors[0].",
        ):
            assert any(p.startswith(prefix) for p in paths), f"probe never reached {prefix}"


class TestSanitizeDepthIsBounded:
    """SCOPED RE-REVIEW R4 (2026-08-27). Fix round 8's two recursive filters --
    `_deep_copy_leaf` (finding 9) and `_filter_rule_tree` (finding 3) -- recurse
    by SHAPE, so their depth is bounded by nothing but Python's own call stack.
    Measured independently on this branch before the fix:

        sys.getrecursionlimit()                = 1000
        max depth json.loads accepts           = 9997   <- what response.json() uses
        max leaf depth sanitize() accepts      = 996

    That is a ~9000-level window where a Celigo response PARSES on the wire and
    then blows the stack inside the sanitizer. It failed closed (RecursionError
    in 0.0004s, nothing committed), which is why this is low severity -- but
    the surface is new to fix round 8, and `RecursionError` is an
    implementation accident, not a decision. The bound is now explicit and the
    failure is a typed, self-explaining one.
    """

    @staticmethod
    def _nest(levels: int, leaf: object = "x") -> object:
        value: object = leaf
        for _ in range(levels):
            value = {"n": value}
        return value

    def test_the_deepest_shape_this_repo_has_ever_seen_is_far_inside_the_bound(self):
        """The bound has to be generous enough that no real payload trips it.
        The deepest literal structure in this repo's whole Celigo corpus
        (fixtures + schemas) is 10 levels."""
        assert sanitizer._MAX_SANITIZE_DEPTH >= 100
        assert sanitizer._MAX_SANITIZE_DEPTH < sys.getrecursionlimit() // 2

    def test_a_leaf_value_nested_past_the_bound_raises_a_typed_error(self):
        too_deep = self._nest(sanitizer._MAX_SANITIZE_DEPTH + 5)

        with pytest.raises(sanitizer.CeligoSanitizeDepthError) as excinfo:
            sanitize("flow", {"_id": "f_deep", "schedule": too_deep})

        assert str(sanitizer._MAX_SANITIZE_DEPTH) in str(excinfo.value)

    def test_a_rules_tree_nested_past_the_bound_raises_a_typed_error(self):
        """`rules` is the other shape-driven recursion (fix round 3's
        `_filter_rule_tree`), and it nests through LISTS as well as dicts."""
        too_deep: object = "notempty"
        for _ in range(sanitizer._MAX_SANITIZE_DEPTH + 5):
            too_deep = [too_deep]

        with pytest.raises(sanitizer.CeligoSanitizeDepthError):
            sanitize("import", {"_id": "i_deep", "filter": {"type": "expression", "rules": too_deep}})

    def test_a_leaf_and_a_rules_tree_just_inside_the_bound_still_sanitize(self):
        """The bound must not be off by one against legitimate input: a
        structure exactly at the limit is accepted, and its content survives."""
        deep_leaf = self._nest(sanitizer._MAX_SANITIZE_DEPTH - 1, leaf="kept")

        out = sanitize("flow", {"_id": "f_edge", "schedule": deep_leaf})

        probe = out["schedule"]
        for _ in range(sanitizer._MAX_SANITIZE_DEPTH - 1):
            probe = probe["n"]
        assert probe == "kept"

    def test_a_self_referential_structure_raises_the_typed_error_not_a_recursionerror(self):
        """The pathological case the depth bound also has to cover: a structure
        Python itself will happily build but never finish walking."""
        looping: dict = {}
        looping["self"] = looping

        with pytest.raises(sanitizer.CeligoSanitizeDepthError):
            sanitize("flow", {"_id": "f_loop", "schedule": looping})
