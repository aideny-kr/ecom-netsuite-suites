"""Tests for docs_link SSE event interception in _intercept_tool_result."""

import json

from app.services.chat.orchestrator import _intercept_tool_result


class TestDocsIntercept:
    def test_intercepts_successful_docs_create(self):
        result_str = json.dumps(
            {
                "error": False,
                "doc_id": "FID",
                "url": "https://docs.google.com/document/d/FID",
                "shared_with": "user@example.com",
                "title": "Q1 Research",
            }
        )
        event_type, event_data, new_result = _intercept_tool_result("docs_create", result_str)
        assert event_type == "docs_link"
        assert event_data["url"] == "https://docs.google.com/document/d/FID"
        assert event_data["doc_id"] == "FID"
        assert event_data["title"] == "Q1 Research"
        assert event_data["shared_with"] == "user@example.com"
        # LLM sees the doc_id but not the full URL (condensed to prevent URL duplication)
        assert "FID" in new_result
        assert "clickable card" in new_result

    def test_ignores_failed_docs_create(self):
        result_str = json.dumps({"error": True, "message": "failed"})
        event_type, _, _ = _intercept_tool_result("docs_create", result_str)
        assert event_type is None

    def test_ignores_missing_url(self):
        result_str = json.dumps({"error": False, "doc_id": "FID"})
        event_type, _, _ = _intercept_tool_result("docs_create", result_str)
        assert event_type is None

    def test_handles_non_json_result(self):
        event_type, _, new_result = _intercept_tool_result("docs_create", "not json")
        assert event_type is None
        assert new_result == "not json"

    def test_matches_dotted_name(self):
        result_str = json.dumps({"error": False, "doc_id": "FID", "url": "https://docs.google.com/document/d/FID"})
        event_type, _, _ = _intercept_tool_result("docs.create", result_str)
        assert event_type == "docs_link"

    def test_defaults_title_when_missing(self):
        result_str = json.dumps({"error": False, "doc_id": "FID", "url": "https://docs.google.com/document/d/FID"})
        event_type, event_data, _ = _intercept_tool_result("docs_create", result_str)
        assert event_type == "docs_link"
        assert event_data["title"] == "Document"

    def test_condenses_result_for_llm(self):
        payload = {
            "error": False,
            "doc_id": "FID",
            "url": "https://docs.google.com/document/d/FID",
            "title": "Q1 Research",
        }
        result_str = json.dumps(payload)
        _, _, new_result = _intercept_tool_result("docs_create", result_str)
        parsed = json.loads(new_result)
        assert parsed["success"] is True
        assert parsed["doc_id"] == "FID"
        assert parsed["title"] == "Q1 Research"
        assert "clickable card" in parsed["note"]
        assert "do NOT paste the URL" in parsed["note"]
