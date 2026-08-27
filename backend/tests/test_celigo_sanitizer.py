"""Celigo payload sanitizer -- allowlist, never denylist.

See docs/superpowers/specs/2026-08-25-celigo-flow-map-design.md §4.3/§6: probed
live on 2026-08-25 against the real Solidus + NetSuite integration, Celigo
objects were found to embed captured production payloads -- a `mockResponse`
contained a live `set-cookie` session header for `.frame.work`, a customer
record, and product data. `exclude` projection on the wire (Task 3) is the
first line of defence; this sanitizer is the second, so a new Celigo field
that slips past projection still can't reach the database.

The fixture below mirrors that REAL shape structurally -- an import object
whose payload-bearing fields carry a cookie header and a customer/product
body -- but every value is INVENTED. No real cookie, email, or order
reference belongs in this repo.
"""

from __future__ import annotations

import copy
import json

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
        raw = {
            "_id": "e1",
            "name": "Solidus Orders Export",
            "adaptorType": "RestExport",
            "_connectionId": "conn1",
            "_sourceId": "src2",
            "sandbox": False,
            "filter": {"type": "expression", "expression": "status == 'complete'"},
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
            "traceKey": "R694979090",
            "errorId": "err1",
            "retryDataKey": "rdk1",
            "source": "pre_save_page_hook",
            "code": "MISSING_SHIP_ADDRESS",
            "message": (
                "MISSING_SHIP_ADDRESS: order R694979090 has no ship_address "
                "in the Solidus payload. Usually a GDPR-scrubbed customer "
                "(email deleted_user_242530@user.deleted) -- check the order."
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
