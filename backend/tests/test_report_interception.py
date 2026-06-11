# backend/tests/test_report_interception.py
import json

from app.services.chat.orchestrator import _intercept_tool_result


def test_report_ready_event_and_condensed_has_no_numbers():
    result = json.dumps({"report_id": "abc", "title": "Q2 Review", "section_count": 5})
    event_type, sse, condensed = _intercept_tool_result("report_compose", result)
    assert event_type == "report_ready"
    assert sse["report_id"] == "abc" and sse["title"] == "Q2 Review"
    assert "url" in sse
    assert "1.2M" not in condensed  # no figures; just title/id/section_count + a 'do not restate' note
