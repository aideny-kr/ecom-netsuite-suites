"""Tests for saved search result interception in orchestrator."""

import json

import pytest

from app.services.chat.orchestrator import _intercept_tool_result, _is_saved_search_tool


class TestIsSavedSearchTool:
    def test_local_saved_search_name(self):
        assert _is_saved_search_tool("netsuite_saved_search") is True

    def test_local_saved_search_dotted(self):
        assert _is_saved_search_tool("netsuite.saved_search") is True

    def test_external_mcp_runsavedsearch(self):
        assert _is_saved_search_tool("ext__abc123__ns_runSavedSearch") is True

    def test_runsavedsearch_direct(self):
        assert _is_saved_search_tool("ns_runSavedSearch") is True

    def test_suiteql_is_not_saved_search(self):
        assert _is_saved_search_tool("netsuite_suiteql") is False

    def test_random_tool_is_not_saved_search(self):
        assert _is_saved_search_tool("rag_search") is False


class TestSavedSearchIntercept:
    def test_list_of_dicts_intercepted(self):
        result = json.dumps(
            {
                "searchId": "customsearch_item_receipts",
                "data": [
                    {"date": "2026-01-01", "item": "Widget A", "quantity": 10},
                    {"date": "2026-01-02", "item": "Widget B", "quantity": 5},
                ],
            }
        )
        event_type, event_data, condensed = _intercept_tool_result("ext__abc123__ns_runSavedSearch", result)
        assert event_type == "data_table"
        assert event_data["columns"] == ["date", "item", "quantity"]
        assert len(event_data["rows"]) == 2
        assert event_data["query"] == "Saved Search: customsearch_item_receipts"

    def test_items_key_intercepted(self):
        result = json.dumps(
            {
                "items": [{"name": "A", "value": 1}],
            }
        )
        event_type, event_data, _ = _intercept_tool_result("ns_runSavedSearch", result)
        assert event_type == "data_table"
        assert event_data["columns"] == ["name", "value"]

    def test_results_key_intercepted(self):
        result = json.dumps(
            {
                "results": [{"col1": "x", "col2": "y"}],
            }
        )
        event_type, event_data, _ = _intercept_tool_result("ns_runSavedSearch", result)
        assert event_type == "data_table"

    def test_error_passes_through(self):
        result = json.dumps({"error": True, "message": "Search not found"})
        event_type, _, _ = _intercept_tool_result("ns_runSavedSearch", result)
        assert event_type is None

    def test_string_error_passes_through(self):
        result = json.dumps({"error": "Invalid search ID"})
        event_type, _, _ = _intercept_tool_result("ns_runSavedSearch", result)
        assert event_type is None

    def test_empty_data_passes_through(self):
        result = json.dumps({"data": []})
        event_type, _, _ = _intercept_tool_result("ns_runSavedSearch", result)
        assert event_type is None

    def test_malformed_json_passes_through(self):
        event_type, _, result_str = _intercept_tool_result("ns_runSavedSearch", "not json")
        assert event_type is None
        assert result_str == "not json"

    def test_condensed_has_note(self):
        result = json.dumps(
            {
                "data": [{"x": 1}],
            }
        )
        _, _, condensed = _intercept_tool_result("ns_runSavedSearch", result)
        parsed = json.loads(condensed)
        assert "note" in parsed

    def test_truncated_flag_preserved(self):
        result = json.dumps(
            {
                "data": [{"x": 1}],
                "truncated": True,
            }
        )
        _, event_data, _ = _intercept_tool_result("ns_runSavedSearch", result)
        assert event_data["truncated"] is True
