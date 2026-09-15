"""Bounded producer views leave original receipts and errors intact."""

import json

from app.services.chat.agents.base_agent import _truncate_tool_result


def test_arbitrary_plugin_can_supply_bounded_view_and_detail_reference():
    result = {
        "success": True,
        "raw_rows": ["private detail"] * 1000,
        "model_context": {
            "version": 1,
            "data": {"summary": "One observed result", "detail_access": {"observation_id": "receipt"}},
        },
    }
    original = json.dumps(result)
    projected = json.loads(_truncate_tool_result(original))
    assert "raw_rows" not in projected
    assert projected["detail_access"]["observation_id"] == "receipt"
    assert "not the complete tool result" in projected["detail_projection"]
    assert json.loads(original) == result


def test_model_view_cannot_hide_original_tool_failure():
    for result in ({"error": "denied"}, {"success": False}):
        result["model_context"] = {"version": 1, "data": {"success": True, "summary": "Posted"}}
        projected = json.loads(_truncate_tool_result(json.dumps(result)))
        assert projected.get("error") == "denied" or projected["success"] is False
        assert "summary" not in projected


def test_unknown_or_oversized_view_preserves_existing_path():
    for view in (
        {"version": 2, "data": {}},
        {"version": 1, "data": "invalid"},
        {"version": 1, "data": {"text": "x" * 24001}},
    ):
        result = json.dumps({"success": True, "model_context": view})
        assert _truncate_tool_result(result) == result
