"""Tests for write_confirmation_service — builds and validates HITL write
confirmation payloads for the NetSuite AI agent."""

from __future__ import annotations

import pytest

from app.services.chat.write_confirmation_service import (
    WriteConfirmationPayload,
    build_confirmation_payload,
    validate_and_extract_confirmation,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEX_32 = "a1b2c3d4e5f67890a1b2c3d4e5f67890"  # 32 hex chars
_SESSION_ID = "session-abc-123"


def _ext(tool_name: str) -> str:
    return f"ext__{_HEX_32}__{tool_name}"


# ---------------------------------------------------------------------------
# WriteConfirmationPayload model defaults
# ---------------------------------------------------------------------------


class TestWriteConfirmationPayloadDefaults:
    def test_type_default(self):
        payload = WriteConfirmationPayload(
            mutation_type="create",
            record_type="salesOrder",
            proposed_fields={},
            tool_name=_ext("ns_createRecord"),
            tool_input={},
            confirmation_token="abc",
        )
        assert payload.type == "write_confirmation"

    def test_status_default(self):
        payload = WriteConfirmationPayload(
            mutation_type="create",
            record_type="salesOrder",
            proposed_fields={},
            tool_name=_ext("ns_createRecord"),
            tool_input={},
            confirmation_token="abc",
        )
        assert payload.status == "pending"

    def test_optional_fields_default_none(self):
        payload = WriteConfirmationPayload(
            mutation_type="create",
            record_type="salesOrder",
            proposed_fields={},
            tool_name=_ext("ns_createRecord"),
            tool_input={},
            confirmation_token="abc",
        )
        assert payload.record_id is None
        assert payload.current_record is None


# ---------------------------------------------------------------------------
# build_confirmation_payload — create
# ---------------------------------------------------------------------------


class TestBuildConfirmationPayloadCreate:
    def test_returns_payload_for_create(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {
            "recordType": "salesOrder",
            "body": {"entity": "123", "memo": "test order"},
        }
        result = build_confirmation_payload(
            mutation_type="create",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
        )
        assert result is not None
        assert isinstance(result, WriteConfirmationPayload)

    def test_correct_type_field(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "salesOrder", "body": {"memo": "test"}}
        result = build_confirmation_payload(
            mutation_type="create",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
        )
        assert result.type == "write_confirmation"

    def test_correct_record_type(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "salesOrder", "body": {"memo": "test"}}
        result = build_confirmation_payload(
            mutation_type="create",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
        )
        assert result.record_type == "salesOrder"

    def test_proposed_fields_from_body(self):
        tool_name = _ext("ns_createRecord")
        body = {"entity": "123", "memo": "test order", "subsidiary": "1"}
        tool_input = {"recordType": "salesOrder", "body": body}
        result = build_confirmation_payload(
            mutation_type="create",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
        )
        assert result.proposed_fields == body

    def test_non_empty_confirmation_token(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "salesOrder", "body": {"memo": "test"}}
        result = build_confirmation_payload(
            mutation_type="create",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
        )
        assert isinstance(result.confirmation_token, str)
        assert len(result.confirmation_token) > 0

    def test_confirmation_token_is_64_char_hex(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "salesOrder", "body": {"memo": "test"}}
        result = build_confirmation_payload(
            mutation_type="create",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
        )
        assert len(result.confirmation_token) == 64
        int(result.confirmation_token, 16)  # raises ValueError if not valid hex

    def test_record_id_is_none_for_create(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "salesOrder", "body": {"memo": "test"}}
        result = build_confirmation_payload(
            mutation_type="create",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
        )
        assert result.record_id is None

    def test_status_is_pending(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "salesOrder", "body": {"memo": "test"}}
        result = build_confirmation_payload(
            mutation_type="create",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
        )
        assert result.status == "pending"

    def test_tool_name_preserved(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "salesOrder", "body": {"memo": "test"}}
        result = build_confirmation_payload(
            mutation_type="create",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
        )
        assert result.tool_name == tool_name

    def test_tool_input_preserved(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "salesOrder", "body": {"memo": "test"}}
        result = build_confirmation_payload(
            mutation_type="create",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
        )
        assert result.tool_input == tool_input

    def test_empty_body_yields_empty_proposed_fields(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "salesOrder"}
        result = build_confirmation_payload(
            mutation_type="create",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
        )
        assert result.proposed_fields == {}


# ---------------------------------------------------------------------------
# build_confirmation_payload — update with before/after snapshot
# ---------------------------------------------------------------------------


class TestBuildConfirmationPayloadUpdate:
    def test_returns_payload_for_update(self):
        tool_name = _ext("ns_updateRecord")
        tool_input = {
            "recordType": "salesOrder",
            "id": "SO-1001",
            "body": {"memo": "updated memo"},
        }
        current = {"id": "SO-1001", "memo": "old memo", "status": "pendingFulfillment"}
        result = build_confirmation_payload(
            mutation_type="update",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
            current_record=current,
        )
        assert result is not None

    def test_current_record_preserved(self):
        tool_name = _ext("ns_updateRecord")
        tool_input = {
            "recordType": "salesOrder",
            "id": "SO-1001",
            "body": {"memo": "updated"},
        }
        current = {"id": "SO-1001", "memo": "original", "status": "pendingFulfillment"}
        result = build_confirmation_payload(
            mutation_type="update",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
            current_record=current,
        )
        assert result.current_record == current

    def test_proposed_fields_from_body_for_update(self):
        tool_name = _ext("ns_updateRecord")
        body = {"memo": "updated memo", "shipDate": "2026-05-01"}
        tool_input = {"recordType": "salesOrder", "id": "SO-1001", "body": body}
        result = build_confirmation_payload(
            mutation_type="update",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
            current_record={"id": "SO-1001", "memo": "old"},
        )
        assert result.proposed_fields == body

    def test_record_id_from_tool_input_id(self):
        tool_name = _ext("ns_updateRecord")
        tool_input = {
            "recordType": "salesOrder",
            "id": "SO-1001",
            "body": {"memo": "updated"},
        }
        result = build_confirmation_payload(
            mutation_type="update",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
        )
        assert result.record_id == "SO-1001"

    def test_record_id_from_body_id_fallback(self):
        tool_name = _ext("ns_updateRecord")
        tool_input = {
            "recordType": "salesOrder",
            "body": {"id": "SO-9999", "memo": "updated"},
        }
        result = build_confirmation_payload(
            mutation_type="update",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
        )
        assert result.record_id == "SO-9999"

    def test_tool_level_id_takes_precedence_over_body_id(self):
        tool_name = _ext("ns_updateRecord")
        tool_input = {
            "recordType": "salesOrder",
            "id": "SO-TOP-LEVEL",
            "body": {"id": "SO-BODY-LEVEL", "memo": "test"},
        }
        result = build_confirmation_payload(
            mutation_type="update",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
        )
        assert result.record_id == "SO-TOP-LEVEL"

    def test_mutation_type_is_update(self):
        tool_name = _ext("ns_updateRecord")
        tool_input = {"recordType": "salesOrder", "id": "SO-1", "body": {}}
        result = build_confirmation_payload(
            mutation_type="update",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
        )
        assert result.mutation_type == "update"


# ---------------------------------------------------------------------------
# build_confirmation_payload — blocked record type
# ---------------------------------------------------------------------------


class TestBuildConfirmationPayloadBlocked:
    @pytest.mark.parametrize(
        "record_type",
        [
            "employee",
            "role",
            "subsidiary",
            "department",
            "classification",
            "location",
            "account",
            "accountingPeriod",
            "customRecordType",
            "script",
            "workflow",
            "integration",
        ],
    )
    def test_blocked_type_returns_none(self, record_type: str):
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": record_type, "body": {"name": "test"}}
        result = build_confirmation_payload(
            mutation_type="create",
            record_type=record_type,
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
        )
        assert result is None

    def test_unknown_type_allowed_with_blocklist(self):
        """Non-blocked types go through HITL confirmation."""
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "customWidget", "body": {"name": "test"}}
        result = build_confirmation_payload(
            mutation_type="create",
            record_type="customWidget",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
        )
        assert result is not None
        assert result.record_type == "customWidget"


# ---------------------------------------------------------------------------
# validate_and_extract_confirmation — round-trip
# ---------------------------------------------------------------------------


class TestValidateAndExtractConfirmation:
    def _make_structured_output(
        self,
        mutation_type: str = "create",
        record_type: str = "salesOrder",
    ) -> dict:
        tool_name = _ext("ns_createRecord")
        tool_input = {
            "recordType": record_type,
            "body": {"entity": "123", "memo": "test order"},
        }
        payload = build_confirmation_payload(
            mutation_type=mutation_type,
            record_type=record_type,
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
        )
        assert payload is not None
        return {
            "type": payload.type,
            "confirmation_token": payload.confirmation_token,
            "tool_name": payload.tool_name,
            "tool_input": payload.tool_input,
        }

    def test_round_trip_returns_valid(self):
        structured_output = self._make_structured_output()
        is_valid, tool_name, tool_input = validate_and_extract_confirmation(structured_output, _SESSION_ID)
        assert is_valid is True

    def test_round_trip_returns_correct_tool_name(self):
        structured_output = self._make_structured_output()
        _, tool_name, _ = validate_and_extract_confirmation(structured_output, _SESSION_ID)
        assert tool_name == _ext("ns_createRecord")

    def test_round_trip_returns_correct_tool_input(self):
        tool_name = _ext("ns_createRecord")
        tool_input = {
            "recordType": "salesOrder",
            "body": {"entity": "123", "memo": "test order"},
        }
        payload = build_confirmation_payload(
            mutation_type="create",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
        )
        assert payload is not None
        structured_output = {
            "confirmation_token": payload.confirmation_token,
            "tool_name": payload.tool_name,
            "tool_input": payload.tool_input,
        }
        _, _, extracted_input = validate_and_extract_confirmation(structured_output, _SESSION_ID)
        assert extracted_input == tool_input

    def test_tampered_token_fails(self):
        structured_output = self._make_structured_output()
        # Replace token with a tampered version
        structured_output["confirmation_token"] = "a" * 64
        is_valid, _, _ = validate_and_extract_confirmation(structured_output, _SESSION_ID)
        assert is_valid is False

    def test_wrong_session_id_fails(self):
        structured_output = self._make_structured_output()
        is_valid, _, _ = validate_and_extract_confirmation(structured_output, "wrong-session-id")
        assert is_valid is False

    def test_tampered_tool_input_fails(self):
        """Changing tool_input after building payload invalidates the token."""
        structured_output = self._make_structured_output()
        # Tamper with the tool_input
        structured_output["tool_input"] = {
            "recordType": "salesOrder",
            "body": {"memo": "TAMPERED"},
        }
        is_valid, _, _ = validate_and_extract_confirmation(structured_output, _SESSION_ID)
        assert is_valid is False

    def test_different_sessions_give_different_tokens(self):
        """Payload built for session A is invalid for session B."""
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "salesOrder", "body": {"memo": "test"}}
        payload_a = build_confirmation_payload(
            mutation_type="create",
            record_type="salesOrder",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id="session-A",
        )
        assert payload_a is not None
        structured_output = {
            "confirmation_token": payload_a.confirmation_token,
            "tool_name": payload_a.tool_name,
            "tool_input": payload_a.tool_input,
        }
        # Validate against a different session
        is_valid, _, _ = validate_and_extract_confirmation(structured_output, "session-B")
        assert is_valid is False

    def test_update_round_trip(self):
        """Update payload validates correctly end-to-end."""
        tool_name = _ext("ns_updateRecord")
        tool_input = {
            "recordType": "invoice",
            "id": "INV-42",
            "body": {"memo": "corrected"},
        }
        payload = build_confirmation_payload(
            mutation_type="update",
            record_type="invoice",
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=_SESSION_ID,
            current_record={"id": "INV-42", "memo": "original"},
        )
        assert payload is not None
        structured_output = {
            "confirmation_token": payload.confirmation_token,
            "tool_name": payload.tool_name,
            "tool_input": payload.tool_input,
        }
        is_valid, extracted_tool_name, extracted_input = validate_and_extract_confirmation(
            structured_output, _SESSION_ID
        )
        assert is_valid is True
        assert extracted_tool_name == tool_name
        assert extracted_input == tool_input
