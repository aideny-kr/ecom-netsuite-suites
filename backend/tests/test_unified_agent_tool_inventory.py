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


def test_unified_agent_setup_uses_connector_gated_tools():
    """_setup_context must use build_all_tool_definitions (connector-gated)
    not build_local_tool_definitions (un-gated) so the LLM never sees a tool
    its tenant doesn't have."""
    import inspect

    from app.services.chat.agents import unified_agent

    src = inspect.getsource(
        unified_agent._setup_context
        if hasattr(unified_agent, "_setup_context")
        else unified_agent.UnifiedAgent._setup_context
    )
    assert "build_all_tool_definitions" in src, (
        "_setup_context must call build_all_tool_definitions (connector-gated) "
        "to populate self._tool_defs, not build_local_tool_definitions."
    )
