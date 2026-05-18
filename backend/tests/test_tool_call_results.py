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


def test_workspace_propose_patch_summary_strips_diff_preview():
    """Codex adversarial review #1: SuiteScript file contents must not leak
    into ChatMessage.tool_calls / LLM history / frontend payloads. The raw
    propose_patch result includes up to 32KB of original_content and
    modified_content; the summary must allowlist action-relevant fields only.
    """
    result = {
        "changeset_id": "abc-123",
        "patch_id": "p-1",
        "operation": "modify",
        "diff_status": "ok",
        "risk_summary": "low",
        "diff_preview": {
            "file_path": "SuiteScripts/auth.js",
            "original_content": "const API_TOKEN = 'sk-live-SECRET-DO-NOT-LEAK';",
            "modified_content": "const API_TOKEN = 'sk-live-STILL-SECRET';",
        },
        "row_count": 1,
    }
    summary = summarize_tool_result("workspace_propose_patch", json.dumps(result))
    parsed = json.loads(summary)
    assert "diff_preview" not in parsed
    assert "original_content" not in summary
    assert "modified_content" not in summary
    assert "SECRET" not in summary
    # action-relevant fields preserved
    assert parsed["changeset_id"] == "abc-123"
    assert parsed["patch_id"] == "p-1"


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
