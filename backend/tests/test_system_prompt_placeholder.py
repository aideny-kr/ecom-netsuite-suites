"""Guard: base system prompt must use the {{TOOL_INVENTORY}} placeholder
instead of a hardcoded tool enumeration."""

from app.services.chat import prompts


class TestSystemPromptPlaceholder:
    def test_prompt_contains_tool_inventory_placeholder(self):
        assert "{{TOOL_INVENTORY}}" in prompts.SYSTEM_PROMPT, (
            "Base system prompt must inject tools dynamically via "
            "{{TOOL_INVENTORY}}, not hardcode a numbered list."
        )

    def test_prompt_does_not_hardcode_netsuite_suiteql_description(self):
        # The old hardcoded line was: "1. netsuite_suiteql — Execute a SuiteQL query …"
        # If anyone reintroduces it, this test catches the drift.
        assert "1. netsuite_suiteql" not in prompts.SYSTEM_PROMPT

    def test_prompt_does_not_hardcode_workspace_propose_patch(self):
        assert "12. workspace_propose_patch" not in prompts.SYSTEM_PROMPT
