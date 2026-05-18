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

    def test_workspace_read_before_patch(self):
        """Prompt should say to always read before patching."""
        prompt = _make_agent().system_prompt
        assert "read before patching" in prompt.lower() or "Always read before patching" in prompt


class TestDuplicatePatchPrevention:
    """Code-level dedup: second propose_patch for same file should be skipped."""

    def test_patched_files_tracked(self):
        """base_agent should have _PATCH_DEDUP logic."""
        from app.services.chat.agents import base_agent

        source = open(base_agent.__file__).read()
        assert "patched_files" in source or "_patched_files" in source

    def test_orchestrator_single_agent_loop_dedups_patches(self):
        """orchestrator's single-agent loop must mirror the dedup.

        Workspace sessions take the single-agent path (multi-agent branch is
        gated by `not workspace_context`), so without dedup here the LLM can
        emit two identical workspace_propose_patch tool_use blocks in one
        response and the user ends up with two draft changesets. Staging
        2026-05-18 hit this — see `403dba46` / `580af610` with identical
        timestamps for SuiteScripts/Uncategorized/FW_reProcessSecondarySub_MR.js.
        """
        from app.services.chat import orchestrator

        source = open(orchestrator.__file__).read()
        # Look in the single-agent loop section (after the multi-agent path
        # bails out via `return` on its `yield message`).
        single_agent_start = source.index("Single-agent agentic loop")
        single_agent_section = source[single_agent_start:]
        assert "patched_files" in single_agent_section
        assert "Skipping duplicate patch" in single_agent_section

    def test_orchestrator_dedup_records_after_success(self):
        """Codex review #2: dedup must record after successful execution.

        Recording the patch key BEFORE execute_tool_call means a transient
        failure (policy block, parse error, DB error) on the first call
        silently skips a corrected retry in the same turn. The fix records
        AFTER parsing the result and confirming a real changeset_id.
        """
        from app.services.chat import orchestrator

        source = open(orchestrator.__file__).read()
        single_agent_section = source[source.index("Single-agent agentic loop") :]
        record_block = "patched_files[_dedup_patch_key] = _new_cs_id"
        assert record_block in single_agent_section
        record_offset = single_agent_section.index(record_block)
        log_offset = single_agent_section.index("tool_calls_log.append")
        assert record_offset > log_offset, "dedup must be recorded AFTER tool execution + logging"
        assert "not _had_error" in single_agent_section
        assert '"changeset_id"' in single_agent_section

    def test_orchestrator_dedup_normalizes_path(self):
        """Codex review #3: dedup must canonicalize file_path.

        Without normalization, the LLM bypasses the guard by varying the
        prefix ('./foo.js' vs 'foo.js'). Use workspace_service.validate_path
        for the dedup key so it matches the path the DB layer stores.
        """
        from app.services.chat import orchestrator

        source = open(orchestrator.__file__).read()
        single_agent_section = source[source.index("Single-agent agentic loop") :]
        assert "validate_path as _ws_validate_path" in single_agent_section
        assert "_ws_validate_path(_raw_patch_path)" in single_agent_section
