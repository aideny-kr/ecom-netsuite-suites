"""Tests for netsuite_financial_report availability in unified agent."""

import uuid


def test_financial_report_in_unified_tool_names():
    """netsuite_financial_report must be in the unified agent's tool set."""
    from app.services.chat.agents.unified_agent import _UNIFIED_TOOL_NAMES

    assert "netsuite_financial_report" in _UNIFIED_TOOL_NAMES


def test_unified_agent_tool_definitions_include_financial_report():
    """Unified agent should build tool definitions that include netsuite_financial_report."""
    from app.services.chat.agents.unified_agent import UnifiedAgent

    agent = UnifiedAgent(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        correlation_id="test",
    )
    tool_names = [t["name"] for t in agent.tool_definitions]
    assert "netsuite_financial_report" in tool_names


def test_unified_agent_system_prompt_mentions_financial_report_tool():
    """The unified agent's system prompt should guide usage of netsuite_financial_report."""
    from app.services.chat.agents.unified_agent import UnifiedAgent

    agent = UnifiedAgent(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        correlation_id="test",
    )
    prompt = agent.system_prompt
    assert "netsuite_financial_report" in prompt
