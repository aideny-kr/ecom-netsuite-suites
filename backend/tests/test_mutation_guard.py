"""Tests for mutation_guard — detects write-path MCP tools and generates
HMAC confirmation tokens."""

from __future__ import annotations

import pytest

from app.services.chat.mutation_guard import (
    generate_confirmation_token,
    get_mutation_type,
    is_mutation_tool,
    is_record_type_allowed,
    verify_confirmation_token,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEX_32 = "a1b2c3d4e5f67890a1b2c3d4e5f67890"  # 32 hex chars


def _ext(tool_name: str) -> str:
    return f"ext__{_HEX_32}__{tool_name}"


# ---------------------------------------------------------------------------
# is_mutation_tool
# ---------------------------------------------------------------------------


class TestIsMutationTool:
    def test_create_record_is_mutation(self):
        assert is_mutation_tool(_ext("ns_createRecord")) is True

    def test_update_record_is_mutation(self):
        assert is_mutation_tool(_ext("ns_updateRecord")) is True

    def test_delete_record_is_mutation(self):
        assert is_mutation_tool(_ext("ns_deleteRecord")) is True

    def test_upsert_record_is_mutation(self):
        assert is_mutation_tool(_ext("ns_upsertRecord")) is True

    def test_get_record_is_not_mutation(self):
        assert is_mutation_tool(_ext("ns_getRecord")) is False

    def test_run_suiteql_is_not_mutation(self):
        assert is_mutation_tool(_ext("ns_runCustomSuiteQL")) is False

    def test_run_report_is_not_mutation(self):
        assert is_mutation_tool(_ext("ns_runReport")) is False

    def test_list_saved_searches_is_not_mutation(self):
        assert is_mutation_tool(_ext("ns_listSavedSearches")) is False

    def test_local_tool_is_not_mutation(self):
        assert is_mutation_tool("netsuite_suiteql") is False

    def test_bigquery_tool_is_not_mutation(self):
        assert is_mutation_tool("bigquery_sql") is False

    def test_rag_search_is_not_mutation(self):
        assert is_mutation_tool("rag_search") is False

    def test_bare_create_record_without_prefix_is_not_mutation(self):
        # Must have the ext__ prefix to be a mutation
        assert is_mutation_tool("ns_createRecord") is False

    def test_partial_prefix_is_not_mutation(self):
        assert is_mutation_tool("ext__ns_createRecord") is False

    def test_wrong_hex_length_is_not_mutation(self):
        # Only 16 hex chars — invalid prefix format
        assert is_mutation_tool("ext__a1b2c3d4e5f67890__ns_createRecord") is False

    def test_different_valid_hex_prefix_is_mutation(self):
        other_hex = "00112233445566778899aabbccddeeff"
        assert is_mutation_tool(f"ext__{other_hex}__ns_updateRecord") is True


# ---------------------------------------------------------------------------
# get_mutation_type
# ---------------------------------------------------------------------------


class TestGetMutationType:
    def test_create(self):
        assert get_mutation_type(_ext("ns_createRecord")) == "create"

    def test_update(self):
        assert get_mutation_type(_ext("ns_updateRecord")) == "update"

    def test_delete(self):
        assert get_mutation_type(_ext("ns_deleteRecord")) == "delete"

    def test_upsert(self):
        assert get_mutation_type(_ext("ns_upsertRecord")) == "upsert"

    def test_read_tool_returns_none(self):
        assert get_mutation_type(_ext("ns_getRecord")) is None

    def test_local_tool_returns_none(self):
        assert get_mutation_type("netsuite_suiteql") is None

    def test_non_mutation_ext_tool_returns_none(self):
        assert get_mutation_type(_ext("ns_runCustomSuiteQL")) is None


# ---------------------------------------------------------------------------
# is_record_type_allowed
# ---------------------------------------------------------------------------


class TestIsRecordTypeAllowed:
    @pytest.mark.parametrize(
        "record_type",
        [
            "salesOrder",
            "customer",
            "inventoryItem",
            "vendor",
            "journalEntry",
            "somethingNew",
        ],
    )
    def test_non_blocked_types_allowed(self, record_type: str):
        assert is_record_type_allowed(record_type) is True

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
    def test_blocked_types(self, record_type: str):
        assert is_record_type_allowed(record_type) is False


# ---------------------------------------------------------------------------
# generate_confirmation_token + verify_confirmation_token
# ---------------------------------------------------------------------------


class TestConfirmationTokens:
    _SESSION_ID = "sess-abc-123"
    _PAYLOAD = '{"recordType": "salesOrder", "action": "create"}'

    def test_round_trip(self):
        token = generate_confirmation_token(self._SESSION_ID, self._PAYLOAD)
        assert verify_confirmation_token(token, self._SESSION_ID, self._PAYLOAD) is True

    def test_wrong_session_id_fails(self):
        token = generate_confirmation_token(self._SESSION_ID, self._PAYLOAD)
        assert verify_confirmation_token(token, "wrong-session", self._PAYLOAD) is False

    def test_wrong_payload_fails(self):
        token = generate_confirmation_token(self._SESSION_ID, self._PAYLOAD)
        tampered = '{"recordType": "salesOrder", "action": "delete"}'
        assert verify_confirmation_token(token, self._SESSION_ID, tampered) is False

    def test_empty_payload(self):
        token = generate_confirmation_token(self._SESSION_ID, "")
        assert verify_confirmation_token(token, self._SESSION_ID, "") is True

    def test_token_is_hex_string(self):
        token = generate_confirmation_token(self._SESSION_ID, self._PAYLOAD)
        # HMAC-SHA256 produces 64 hex chars
        assert len(token) == 64
        int(token, 16)  # raises ValueError if not hex

    def test_different_sessions_produce_different_tokens(self):
        t1 = generate_confirmation_token("session-1", self._PAYLOAD)
        t2 = generate_confirmation_token("session-2", self._PAYLOAD)
        assert t1 != t2

    def test_different_payloads_produce_different_tokens(self):
        t1 = generate_confirmation_token(self._SESSION_ID, '{"a": 1}')
        t2 = generate_confirmation_token(self._SESSION_ID, '{"a": 2}')
        assert t1 != t2


# ---------------------------------------------------------------------------
# Celigo write verbs
# ---------------------------------------------------------------------------


class TestCeligoMutationClassification:
    """Celigo write tools must classify as mutations.

    Regression guard: before this, classify_mutation() returned None for every
    Celigo verb, so `delete_resource` would have auto-executed with no HITL —
    violating CLAUDE.md mistake #2. Celigo's own delete_resource never blocks
    server-side, so this cannot be left to the remote server.
    """

    CONNECTOR_HEX = "a" * 32

    def _ext(self, raw: str) -> str:
        return f"ext__{self.CONNECTOR_HEX}__{raw}"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("upsert_flow", "upsert"),
            ("upsert_script", "upsert"),
            ("patch_flow", "update"),
            ("delete_resource", "delete"),
            ("delete_lookup_cache_data", "delete"),
            ("run_flow", "update"),
            ("triage_flow_errors", "update"),
            ("manage_user", "update"),
        ],
    )
    def test_celigo_writes_classify(self, raw, expected):
        from app.services.chat.mutation_guard import classify_mutation

        assert classify_mutation(self._ext(raw)) == expected

    @pytest.mark.parametrize("raw", ["list_flows", "list_scripts", "get_schema", "search_knowledge_base"])
    def test_celigo_reads_are_not_mutations(self, raw):
        from app.services.chat.mutation_guard import classify_mutation

        assert classify_mutation(self._ext(raw)) is None

    def test_netsuite_verbs_still_classify(self):
        """Adding Celigo verbs must not disturb the existing NetSuite mapping."""
        from app.services.chat.mutation_guard import classify_mutation

        assert classify_mutation(self._ext("ns_createRecord")) == "create"
        assert classify_mutation(self._ext("ns_deleteRecord")) == "delete"
