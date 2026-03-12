"""Tests for netsuite_financial_report availability in unified agent."""


def test_financial_report_in_unified_tool_names():
    """netsuite_financial_report must be in the unified agent's tool set."""
    from app.services.chat.agents.unified_agent import _UNIFIED_TOOL_NAMES

    assert "netsuite_financial_report" in _UNIFIED_TOOL_NAMES


def test_unified_agent_tool_definitions_include_financial_report():
    """Unified agent should build tool definitions that include netsuite_financial_report."""
    from app.services.chat.agents.unified_agent import UnifiedAgent

    agent = UnifiedAgent.__new__(UnifiedAgent)
    agent._tool_defs = None

    tool_names = [t["name"] for t in agent.tool_definitions]
    assert "netsuite_financial_report" in tool_names


def test_unified_agent_system_prompt_mentions_financial_report_tool():
    """The unified agent's system prompt should guide usage of netsuite_financial_report."""
    from app.services.chat.agents.unified_agent import UnifiedAgent

    agent = UnifiedAgent.__new__(UnifiedAgent)
    agent.tenant_id = None
    agent.user_id = None
    agent._correlation_id = "test"
    agent._metadata = None
    agent._policy = None
    agent._tool_defs = None
    agent._tenant_vernacular = ""
    agent._soul_quirks = ""
    agent._soul_tone = ""
    agent._brand_name = ""
    agent._user_timezone = None
    agent._current_task = ""
    agent._domain_knowledge = []
    agent._proven_patterns = []
    agent._active_skill = None
    agent._context = {}

    prompt = agent.system_prompt
    assert "netsuite_financial_report" in prompt
