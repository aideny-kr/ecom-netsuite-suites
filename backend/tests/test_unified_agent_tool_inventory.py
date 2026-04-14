"""UnifiedAgent must not carry its own hardcoded tool list.

_UNIFIED_TOOL_NAMES was a frozenset of ten tool names used nowhere except
to lie to the LLM — bigquery_sql was missing even though the main orchestrator
path added it to the tool schema when a BigQuery connector was active."""


def test_unified_agent_has_no_hardcoded_tool_names_frozenset():
    from app.services.chat.agents import unified_agent

    assert not hasattr(unified_agent, "_UNIFIED_TOOL_NAMES"), (
        "Delete the _UNIFIED_TOOL_NAMES frozenset. Tool visibility is "
        "derived from build_all_tool_definitions at runtime."
    )


def test_unified_agent_system_prompt_uses_placeholder():
    from app.services.chat.agents import unified_agent

    assert "{{TOOL_INVENTORY}}" in unified_agent._SYSTEM_PROMPT
