"""Tests for summarize_tool_result.

Regression: workspace_propose_patch must preserve changeset_id JSON in the
result_summary so the frontend ChangeProposalCard can render approve/reject
buttons. Collapsing it to "Returned 1 row" leaves the user without actions —
which is the "AI Chat doesn't work" report from staging 2026-05-18.
"""

from __future__ import annotations

import json

from app.services.chat.tool_call_results import summarize_tool_result


def test_workspace_propose_patch_preserves_changeset_id_json():
    result = {
        "changeset_id": "403dba46-76d1-49fc-8f17-c40e5c5dead7",
        "patch_id": "abc-123",
        "operation": "modify",
        "diff_status": "ok",
        "risk_summary": "low",
        "row_count": 1,
    }
    summary = summarize_tool_result("workspace_propose_patch", json.dumps(result))
    parsed = json.loads(summary)
    assert parsed["changeset_id"] == "403dba46-76d1-49fc-8f17-c40e5c5dead7"
    assert parsed["operation"] == "modify"


def test_workspace_propose_patch_error_still_summarized():
    # Errors should still go through the normal error path
    err = {"error": "Permission denied", "row_count": 0}
    summary = summarize_tool_result("workspace_propose_patch", json.dumps(err))
    assert summary == "Permission denied"


def test_other_tools_still_summarized_to_row_count():
    # workspace_list_files should keep the compact summary
    result = {"files": [{"id": "1"}], "row_count": 1}
    summary = summarize_tool_result("workspace_list_files", json.dumps(result))
    assert summary == "Returned 1 row"
