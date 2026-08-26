"""FIX 4 (round 2 T2 gate on PR #202): _build_role_prompt's provider if/elif
chain handled netsuite_mcp/shopify_mcp/stripe_mcp/custom but was never
extended for celigo_mcp, so a Celigo-only tenant's agent falls through the
chain silently and the role prompt's "connected systems" summary never
mentions Celigo -- even though _PROVIDER_DESCRIPTIONS["celigo_mcp"]
(test_celigo_provider_description.py) already has a full description used
elsewhere in the prompt (_build_connected_systems_block).
"""

from types import SimpleNamespace

from app.services.chat.agents.unified_agent import _build_role_prompt


def _connector(provider: str, label: str = "Celigo") -> SimpleNamespace:
    return SimpleNamespace(provider=provider, label=label)


class TestCeligoRolePrompt:
    def test_celigo_only_tenant_mentions_celigo(self):
        prompt = _build_role_prompt([_connector("celigo_mcp")], brand_name="Acme")
        assert "Celigo" in prompt

    def test_celigo_alongside_netsuite_mentions_both(self):
        prompt = _build_role_prompt(
            [_connector("netsuite_mcp", "NetSuite"), _connector("celigo_mcp")],
            brand_name="Acme",
        )
        assert "NetSuite" in prompt
        assert "Celigo" in prompt

    def test_does_not_hardcode_tool_names(self):
        """Tool names come from {{TOOL_INVENTORY}}; hardcoding drifts.

        CI invariant tests/test_prompt_tool_sync.py enforces this repo-wide.
        """
        prompt = _build_role_prompt([_connector("celigo_mcp")], brand_name="Acme")
        for name in ("list_flows", "list_scripts", "get_schema", "upsert_flow"):
            assert name not in prompt
