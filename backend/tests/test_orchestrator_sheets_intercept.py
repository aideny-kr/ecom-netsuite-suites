"""Tests for sheets_link SSE event interception in _intercept_tool_result."""

import json

from app.services.chat.orchestrator import _intercept_tool_result


class TestSheetsIntercept:
    def test_intercepts_successful_sheets_create(self):
        result_str = json.dumps(
            {
                "error": False,
                "spreadsheet_id": "abc123",
                "url": "https://docs.google.com/spreadsheets/d/abc123",
                "shared_with": "user@example.com",
                "title": "Sales Export",
            }
        )
        event_type, event_data, new_result = _intercept_tool_result("sheets_create", result_str)
        assert event_type == "sheets_link"
        assert event_data["url"] == "https://docs.google.com/spreadsheets/d/abc123"
        assert event_data["spreadsheet_id"] == "abc123"
        assert event_data["title"] == "Sales Export"
        assert event_data["shared_with"] == "user@example.com"
        # LLM sees the spreadsheet_id but not the full URL (condensed to prevent URL duplication)
        assert "abc123" in new_result
        assert "clickable card" in new_result

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
        result_str = json.dumps(
            {
                "error": False,
                "spreadsheet_id": "abc",
                "url": "https://docs.google.com/spreadsheets/d/abc",
            }
        )
        event_type, _, _ = _intercept_tool_result("sheets.create", result_str)
        assert event_type == "sheets_link"

    def test_defaults_title_when_missing(self):
        result_str = json.dumps(
            {
                "error": False,
                "spreadsheet_id": "xyz",
                "url": "https://docs.google.com/spreadsheets/d/xyz",
            }
        )
        event_type, event_data, _ = _intercept_tool_result("sheets_create", result_str)
        assert event_type == "sheets_link"
        assert event_data["title"] == "Spreadsheet"

    def test_condenses_result_for_llm(self):
        """The LLM receives a condensed result with a 'do not paste URL' note instead of the raw result."""
        payload = {
            "error": False,
            "spreadsheet_id": "abc",
            "url": "https://docs.google.com/spreadsheets/d/abc",
            "title": "My Sheet",
        }
        result_str = json.dumps(payload)
        _, _, new_result = _intercept_tool_result("sheets_create", result_str)
        parsed = json.loads(new_result)
        assert parsed["success"] is True
        assert parsed["spreadsheet_id"] == "abc"
        assert parsed["title"] == "My Sheet"
        assert "clickable card" in parsed["note"]
        assert "do NOT paste the URL" in parsed["note"]
