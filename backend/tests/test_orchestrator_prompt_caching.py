"""Tests that the orchestrator uses prompt caching correctly."""

import pytest

from app.services.chat.prompt_cache import split_system_prompt


class TestOrchestratorPromptCaching:
    """Verify the orchestrator splits and passes prompt parts correctly."""

    def test_split_is_called_with_full_system_prompt(self):
        """Ensure split_system_prompt extracts dynamic blocks from static content."""
        prompt = (
            "You are an assistant.\n\n"
            "<tenant_vernacular>Customer Acme = ID 123</tenant_vernacular>\n\n"
            "<domain_knowledge>GL account info</domain_knowledge>\n\n"
            "Use the tools wisely."
        )
        parts = split_system_prompt(prompt)

        assert "You are an assistant." in parts.static
        assert "Use the tools wisely." in parts.static
        assert "<tenant_vernacular>" not in parts.static
        assert "<domain_knowledge>" not in parts.static
        assert "<tenant_vernacular>" in parts.dynamic
        assert "<domain_knowledge>" in parts.dynamic

    def test_static_part_is_stable_across_different_dynamic_content(self):
        """The static part should be identical regardless of dynamic content.
        This is what makes caching work — same static = same cache key."""
        prompt_v1 = (
            "You are an assistant.\n\n"
            "<tenant_vernacular>Customer Acme = ID 123</tenant_vernacular>\n\n"
            "Use the tools wisely."
        )
        prompt_v2 = (
            "You are an assistant.\n\n"
            "<tenant_vernacular>Customer Beta = ID 456</tenant_vernacular>\n\n"
            "Use the tools wisely."
        )
        parts_v1 = split_system_prompt(prompt_v1)
        parts_v2 = split_system_prompt(prompt_v2)

        # Static parts MUST be identical for cache to hit
        assert parts_v1.static == parts_v2.static
        # Dynamic parts differ (different entity mappings)
        assert parts_v1.dynamic != parts_v2.dynamic

    def test_no_dynamic_blocks_still_works(self):
        """If there are no dynamic XML blocks, static = full prompt, dynamic = empty."""
        prompt = "You are an assistant.\n\nUse the tools wisely."
        parts = split_system_prompt(prompt)

        assert parts.static == prompt
        assert parts.dynamic == ""

    def test_import_exists_in_orchestrator(self):
        """Verify the orchestrator imports split_system_prompt."""
        import inspect
        from app.services.chat import orchestrator

        source = inspect.getsource(orchestrator)
        assert "split_system_prompt" in source

    def test_orchestrator_does_not_pass_raw_system_prompt_to_stream(self):
        """Verify the orchestrator uses prompt_parts, not system_prompt, in stream_message calls."""
        import inspect
        from app.services.chat import orchestrator

        source = inspect.getsource(orchestrator)
        # The single-agent agentic loop should use prompt_parts.static, not system=system_prompt
        # Count occurrences of system=system_prompt — should only appear in non-stream contexts
        # (e.g., coordinator call, not direct adapter.stream_message)
        lines = source.split("\n")
        stream_contexts = []
        for i, line in enumerate(lines):
            if "adapter.stream_message(" in line:
                # Grab the next few lines to check what system= is passed
                context = "\n".join(lines[i:i + 8])
                stream_contexts.append(context)

        for ctx in stream_contexts:
            assert "system=system_prompt" not in ctx, (
                f"adapter.stream_message still uses raw system_prompt:\n{ctx}"
            )
            assert "prompt_parts.static" in ctx
