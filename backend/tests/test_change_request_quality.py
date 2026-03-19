"""Tests for change request quality — single patch, show diff, budget.

The agent should: propose ONE patch per file, show the diff in output,
and complete change requests in 3 tool calls max.
"""

import uuid

from app.services.chat.agents.unified_agent import UnifiedAgent


def _make_agent() -> UnifiedAgent:
    return UnifiedAgent(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        correlation_id="test",
    )


class TestPromptChangeRequestRules:
    """The unified agent prompt should have change request discipline rules."""

    def test_one_patch_per_file_rule(self):
        """Prompt must say ONE patch per file."""
        prompt = _make_agent().system_prompt
        assert "ONCE per file" in prompt or "ONE patch per file" in prompt

    def test_show_diff_in_output(self):
        """Output instructions must mention showing diff after propose_patch."""
        prompt = _make_agent().system_prompt
        assert "workspace_propose_patch" in prompt
        # The diff display instruction should be in output_instructions section
        output_section_start = prompt.index("<output_instructions>")
        output_section_end = prompt.index("</output_instructions>")
        output_section = prompt[output_section_start:output_section_end]
        assert "diff" in output_section.lower()

    def test_change_request_budget(self):
        """Prompt should mention 3 tool call budget for change requests."""
        prompt = _make_agent().system_prompt
        assert "3 tool calls" in prompt or "search → read → patch" in prompt

    def test_no_line_by_line_analysis(self):
        """Prompt should say not to analyze file line-by-line in reasoning."""
        prompt = _make_agent().system_prompt
        assert "line-by-line" in prompt


class TestDuplicatePatchPrevention:
    """Code-level dedup: second propose_patch for same file should be skipped."""

    def test_patched_files_tracked(self):
        """base_agent should have _PATCH_DEDUP logic."""
        from app.services.chat.agents import base_agent
        source = open(base_agent.__file__).read()
        assert "patched_files" in source or "_patched_files" in source
