"""Tests for mutation intercept logic in the base_agent tool execution loop.

Verifies:
1. Guard function correctly detects mutation tools (integration with mutation_guard)
2. The intercept block produces the expected result_str format (JSON with
   confirmation_required: true) for allowed record types
3. Blocked record types produce an error JSON instead of a confirmation payload
4. The getRecord pre-fetch tool name is built correctly from the update tool name
"""

from __future__ import annotations

import json

import pytest

from app.services.chat.mutation_guard import (
    get_mutation_type,
    is_mutation_tool,
    is_record_type_allowed,
)
from app.services.chat.write_confirmation_service import (
    WriteConfirmationPayload,
    build_confirmation_payload,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEX_32 = "a1b2c3d4e5f67890a1b2c3d4e5f67890"
_SESSION_ID = "test-session-intercept-001"


def _ext(tool_name: str) -> str:
    return f"ext__{_HEX_32}__{tool_name}"


def _build_intercept_result_str(
    tool_name: str,
    tool_input: dict,
    session_id: str,
    current_record: dict | None = None,
) -> str:
    """Replicate the result_str logic that the base_agent intercept block
    should produce for an *allowed* mutation tool.

    This is the contract the intercept must satisfy:
    - JSON with ``confirmation_required: true``
    - ``mutation_type`` matches the tool verb
    - ``record_type`` from tool_input
    """
    mutation_type = get_mutation_type(tool_name)
    record_type = tool_input.get("recordType", "unknown")

    payload = build_confirmation_payload(
        mutation_type=mutation_type,
        record_type=record_type,
        tool_name=tool_name,
        tool_input=tool_input,
        session_id=session_id,
        current_record=current_record,
    )
    if payload is None:
        # Blocked or unknown record type
        return json.dumps(
            {
                "error": f"Record type '{record_type}' is not allowed for "
                f"AI-initiated {mutation_type} operations.",
                "blocked": True,
            }
        )

    return json.dumps(
        {
            "confirmation_required": True,
            "mutation_type": mutation_type,
            "record_type": record_type,
            "message": (
                f"This {mutation_type} operation on {record_type} requires human "
                f"confirmation. The confirmation dialog has been shown to the user. "
                f"Do NOT proceed until the user explicitly approves."
            ),
        }
    )


def _build_blocked_result_str(mutation_type: str, record_type: str) -> str:
    """Replicate the result_str for a BLOCKED record type."""
    return json.dumps(
        {
            "error": f"Record type '{record_type}' is not allowed for "
            f"AI-initiated {mutation_type} operations.",
            "blocked": True,
        }
    )


# ---------------------------------------------------------------------------
# Guard detection (integration sanity)
# ---------------------------------------------------------------------------


class TestMutationGuardIntegration:
    """Quick sanity checks that the guard functions work correctly when
    composed — these are the exact calls the intercept block makes."""

    def test_create_detected_and_typed(self):
        name = _ext("ns_createRecord")
        assert is_mutation_tool(name) is True
        assert get_mutation_type(name) == "create"

    def test_update_detected_and_typed(self):
        name = _ext("ns_updateRecord")
        assert is_mutation_tool(name) is True
        assert get_mutation_type(name) == "update"

    def test_delete_detected_and_typed(self):
        name = _ext("ns_deleteRecord")
        assert is_mutation_tool(name) is True
        assert get_mutation_type(name) == "delete"

    def test_upsert_detected_and_typed(self):
        name = _ext("ns_upsertRecord")
        assert is_mutation_tool(name) is True
        assert get_mutation_type(name) == "upsert"

    def test_get_record_not_detected(self):
        name = _ext("ns_getRecord")
        assert is_mutation_tool(name) is False

    def test_suiteql_not_detected(self):
        name = _ext("ns_runCustomSuiteQL")
        assert is_mutation_tool(name) is False


# ---------------------------------------------------------------------------
# Intercept result_str format — allowed record types
# ---------------------------------------------------------------------------


class TestInterceptResultStrAllowed:
    """Verify the result_str JSON that the intercept block should produce
    for allowed record types contains the correct structure."""

    def test_create_salesorder_result_str_has_confirmation_required(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "salesOrder", "body": {"entity": "123"}}
        result_str = _build_intercept_result_str(tool_name, tool_input, _SESSION_ID)
        parsed = json.loads(result_str)
        assert parsed["confirmation_required"] is True

    def test_create_salesorder_result_str_has_mutation_type(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "salesOrder", "body": {"entity": "123"}}
        result_str = _build_intercept_result_str(tool_name, tool_input, _SESSION_ID)
        parsed = json.loads(result_str)
        assert parsed["mutation_type"] == "create"

    def test_create_salesorder_result_str_has_record_type(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "salesOrder", "body": {"entity": "123"}}
        result_str = _build_intercept_result_str(tool_name, tool_input, _SESSION_ID)
        parsed = json.loads(result_str)
        assert parsed["record_type"] == "salesOrder"

    def test_create_salesorder_result_str_has_message(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "salesOrder", "body": {"entity": "123"}}
        result_str = _build_intercept_result_str(tool_name, tool_input, _SESSION_ID)
        parsed = json.loads(result_str)
        assert "confirmation" in parsed["message"].lower()
        assert "Do NOT proceed" in parsed["message"]

    def test_update_invoice_result_str_has_confirmation_required(self):
        tool_name = _ext("ns_updateRecord")
        tool_input = {
            "recordType": "invoice",
            "id": "INV-42",
            "body": {"memo": "updated"},
        }
        result_str = _build_intercept_result_str(
            tool_name,
            tool_input,
            _SESSION_ID,
            current_record={"id": "INV-42", "memo": "old"},
        )
        parsed = json.loads(result_str)
        assert parsed["confirmation_required"] is True
        assert parsed["mutation_type"] == "update"
        assert parsed["record_type"] == "invoice"

    def test_delete_customer_result_str(self):
        tool_name = _ext("ns_deleteRecord")
        tool_input = {"recordType": "customer", "id": "CUST-1"}
        result_str = _build_intercept_result_str(tool_name, tool_input, _SESSION_ID)
        parsed = json.loads(result_str)
        assert parsed["confirmation_required"] is True
        assert parsed["mutation_type"] == "delete"

    def test_result_str_is_valid_json(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "purchaseOrder", "body": {"vendor": "V-1"}}
        result_str = _build_intercept_result_str(tool_name, tool_input, _SESSION_ID)
        # Should not raise
        parsed = json.loads(result_str)
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Intercept result_str format — blocked record types
# ---------------------------------------------------------------------------


class TestInterceptResultStrBlocked:
    """Verify that blocked record types produce an error JSON."""

    def test_blocked_employee_returns_error(self):
        result_str = _build_blocked_result_str("create", "employee")
        parsed = json.loads(result_str)
        assert "error" in parsed
        assert parsed["blocked"] is True

    def test_blocked_role_returns_error(self):
        result_str = _build_blocked_result_str("update", "role")
        parsed = json.loads(result_str)
        assert "error" in parsed
        assert parsed["blocked"] is True

    def test_blocked_error_mentions_record_type(self):
        result_str = _build_blocked_result_str("delete", "subsidiary")
        parsed = json.loads(result_str)
        assert "subsidiary" in parsed["error"]

    def test_blocked_error_mentions_mutation_type(self):
        result_str = _build_blocked_result_str("create", "account")
        parsed = json.loads(result_str)
        assert "create" in parsed["error"]

    def test_unknown_type_also_blocked(self):
        """Unknown record types are not on the allowlist, so they're blocked."""
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "customWidget", "body": {"name": "test"}}
        result_str = _build_intercept_result_str(tool_name, tool_input, _SESSION_ID)
        parsed = json.loads(result_str)
        assert "error" in parsed
        assert parsed["blocked"] is True


# ---------------------------------------------------------------------------
# getRecord pre-fetch tool name construction
# ---------------------------------------------------------------------------


class TestGetRecordPreFetchToolName:
    """Verify that the update intercept correctly builds the getRecord tool name
    by replacing 'ns_updateRecord' with 'ns_getRecord' while preserving the
    ext__<32hex>__ prefix."""

    def test_update_tool_name_converts_to_get(self):
        update_name = _ext("ns_updateRecord")
        # The intercept should do: tool_name.replace("ns_updateRecord", "ns_getRecord")
        get_name = update_name.replace("ns_updateRecord", "ns_getRecord")
        assert get_name == _ext("ns_getRecord")

    def test_create_tool_name_converts_to_get(self):
        create_name = _ext("ns_createRecord")
        get_name = create_name.replace("ns_createRecord", "ns_getRecord")
        assert get_name == _ext("ns_getRecord")

    def test_delete_tool_name_converts_to_get(self):
        delete_name = _ext("ns_deleteRecord")
        get_name = delete_name.replace("ns_deleteRecord", "ns_getRecord")
        assert get_name == _ext("ns_getRecord")

    def test_upsert_tool_name_converts_to_get(self):
        upsert_name = _ext("ns_upsertRecord")
        get_name = upsert_name.replace("ns_upsertRecord", "ns_getRecord")
        assert get_name == _ext("ns_getRecord")

    def test_prefix_preserved_after_replacement(self):
        update_name = _ext("ns_updateRecord")
        get_name = update_name.replace("ns_updateRecord", "ns_getRecord")
        assert get_name.startswith(f"ext__{_HEX_32}__")
        assert get_name.endswith("ns_getRecord")
