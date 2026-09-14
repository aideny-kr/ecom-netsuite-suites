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


def test_schedule_create_preserves_schedule_id_json():
    """Scheduled Jobs platform (Slice 2, Task 7) — the chat hand-off card
    (`schedule-created-card.tsx`) needs `schedule_id` to link "Review the
    plan on Scheduled jobs →" to `/scheduled-jobs/{id}`. Same precedent as
    workspace_propose_patch's changeset_id: allowlist the fields the
    frontend needs rather than let the compact-summary fallback truncate an
    arbitrary-length `summary_line` before the id."""
    result = {
        "schedule_id": "403dba46-76d1-49fc-8f17-c40e5c5dead7",
        "name": "Payout reconciliation weekly",
        "schedule_type": "job",
        "plan_status": "pending_approval",
        # Long enough that the generic result_str[:500] fallback would cut
        # schedule_id off the end if this case fell through to it — pins
        # the allowlist branch, not a lucky short-string truncation.
        "summary_line": "reads Stripe payouts and NetSuite deposits, holds needs-review lines, "
        "emails the exception summary " + ("x" * 500),
    }
    summary = summarize_tool_result("schedule.create", json.dumps(result))
    parsed = json.loads(summary)
    assert parsed["schedule_id"] == "403dba46-76d1-49fc-8f17-c40e5c5dead7"
    assert parsed["plan_status"] == "pending_approval"


def test_schedule_create_clarification_still_summarized_as_error():
    # instruction given, but the compiler asked a question first — nothing
    # was created, so this stays on the ordinary error path (the message
    # IS the clarification question; the agent relays it in its own turn).
    result = {"error": True, "clarification": True, "message": "Which subsidiary?"}
    summary = summarize_tool_result("schedule.create", json.dumps(result))
    assert summary == "Which subsidiary?"


def test_schedule_create_without_schedule_id_falls_through_without_crashing():
    # A shape missing schedule_id (defensive — every real execute_create
    # success path sets it) must not crash the allowlist branch; it just
    # falls through to the ordinary summarization the tool would have gotten
    # without a schedule.create-specific case at all.
    result = {"name": "no id here", "row_count": 0}
    summary = summarize_tool_result("schedule.create", json.dumps(result))
    assert isinstance(summary, str)
    assert "schedule_id" not in summary
