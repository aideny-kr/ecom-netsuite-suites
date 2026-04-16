"""Tests for sheets_link SSE event interception in _intercept_tool_result."""

import json
import pytest

from app.services.chat.orchestrator import _intercept_tool_result


class TestSheetsIntercept:
    def test_intercepts_successful_sheets_create(self):
        result_str = json.dumps({
            "error": False,
            "spreadsheet_id": "abc123",
            "url": "https://docs.google.com/spreadsheets/d/abc123",
            "shared_with": "user@example.com",
            "title": "Sales Export",
        })
        event_type, event_data, new_result = _intercept_tool_result("sheets_create", result_str)
        assert event_type == "sheets_link"
        assert event_data["url"] == "https://docs.google.com/spreadsheets/d/abc123"
        assert event_data["spreadsheet_id"] == "abc123"
        assert event_data["title"] == "Sales Export"
        assert event_data["shared_with"] == "user@example.com"
        # LLM still sees the URL so it can reference it
        assert "abc123" in new_result

    def test_ignores_failed_sheets_create(self):
        result_str = json.dumps({"error": True, "message": "failed"})
        event_type, event_data, new_result = _intercept_tool_result("sheets_create", result_str)
        assert event_type is None

    def test_ignores_missing_url(self):
        result_str = json.dumps({"error": False, "spreadsheet_id": "abc"})
        event_type, event_data, new_result = _intercept_tool_result("sheets_create", result_str)
        assert event_type is None

    def test_handles_non_json_result(self):
        event_type, event_data, new_result = _intercept_tool_result("sheets_create", "not json")
        assert event_type is None
        assert new_result == "not json"

    def test_matches_dotted_name(self):
        """Supports both sheets_create and sheets.create tool names."""
        result_str = json.dumps({
            "error": False,
            "spreadsheet_id": "abc",
            "url": "https://docs.google.com/spreadsheets/d/abc",
        })
        event_type, _, _ = _intercept_tool_result("sheets.create", result_str)
        assert event_type == "sheets_link"

    def test_defaults_title_when_missing(self):
        result_str = json.dumps({
            "error": False,
            "spreadsheet_id": "xyz",
            "url": "https://docs.google.com/spreadsheets/d/xyz",
        })
        event_type, event_data, _ = _intercept_tool_result("sheets_create", result_str)
        assert event_type == "sheets_link"
        assert event_data["title"] == "Spreadsheet"

    def test_passes_result_through_unchanged(self):
        """The LLM must see the full result — we do NOT condense for sheets_link."""
        payload = {
            "error": False,
            "spreadsheet_id": "abc",
            "url": "https://docs.google.com/spreadsheets/d/abc",
            "title": "My Sheet",
        }
        result_str = json.dumps(payload)
        _, _, new_result = _intercept_tool_result("sheets_create", result_str)
        assert new_result == result_str
