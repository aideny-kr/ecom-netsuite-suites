"""The agent needs to know what Celigo tools are for — and what they cannot do."""

from app.services.chat.agents.unified_agent import _PROVIDER_DESCRIPTIONS


class TestCeligoProviderDescription:
    def test_celigo_mcp_has_a_description(self):
        assert "celigo_mcp" in _PROVIDER_DESCRIPTIONS
        assert _PROVIDER_DESCRIPTIONS["celigo_mcp"].strip()

    def test_description_states_read_only(self):
        desc = _PROVIDER_DESCRIPTIONS["celigo_mcp"].lower()
        assert "read-only" in desc

    def test_description_does_not_hardcode_tool_names(self):
        """Tool names come from {{TOOL_INVENTORY}}; hardcoding them drifts.

        CI invariant tests/test_prompt_tool_sync.py enforces this repo-wide.
        """
        desc = _PROVIDER_DESCRIPTIONS["celigo_mcp"]
        for name in ("list_flows", "list_scripts", "get_schema", "upsert_flow"):
            assert name not in desc
