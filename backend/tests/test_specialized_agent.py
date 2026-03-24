"""Tests for SpecializedAgent — composition-based agent driven by YAML config."""

import uuid

from app.services.chat.agents.agent_protocol import AgentProtocol
from app.services.chat.agents.agent_yaml_config import AgentYAMLConfig
from app.services.chat.agents.specialized_agent import SpecializedAgent


def _make_config(**overrides) -> AgentYAMLConfig:
    defaults = {
        "agent_id": "test-agent",
        "display_name": "Test Agent",
        "description": "A test agent",
        "tool_ids": ["netsuite_suiteql", "rag_search"],
        "max_steps": 8,
        "model_preference": None,
        "rag_partitions": ["test-partition"],
        "prompt_file": None,
        "requires_confirmation": False,
        "cost_budget": None,
    }
    defaults.update(overrides)
    return AgentYAMLConfig(**defaults)


def _make_agent(config=None, prompt_text="You are a test agent.", knowledge=None):
    config = config or _make_config()
    return SpecializedAgent(
        config=config,
        prompt_text=prompt_text,
        knowledge=knowledge or [],
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        correlation_id="test-corr",
    )


class TestSpecializedAgentProperties:
    def test_agent_name_delegates_to_config(self):
        agent = _make_agent(_make_config(agent_id="pricing-agent"))
        assert agent.agent_name == "pricing-agent"

    def test_max_steps_delegates_to_config(self):
        agent = _make_agent(_make_config(max_steps=12))
        assert agent.max_steps == 12

    def test_system_prompt_includes_base_prompt_text(self):
        agent = _make_agent(prompt_text="You are a pricing expert.")
        assert "You are a pricing expert." in agent.system_prompt

    def test_system_prompt_injects_knowledge_blocks(self):
        agent = _make_agent(knowledge=["Chunk 1 text", "Chunk 2 text"])
        prompt = agent.system_prompt
        assert "<knowledge>" in prompt
        assert "Chunk 1 text" in prompt
        assert "Chunk 2 text" in prompt

    def test_system_prompt_no_knowledge_when_empty(self):
        agent = _make_agent(knowledge=[])
        assert "<knowledge>" not in agent.system_prompt

    def test_tool_definitions_filters_by_config(self):
        config = _make_config(tool_ids=["netsuite_suiteql", "rag_search"])
        agent = _make_agent(config)
        tools = agent.tool_definitions
        tool_names = {t["name"] for t in tools}
        assert tool_names <= {"netsuite_suiteql", "rag_search"}

    def test_tool_definitions_unified_agent_gets_all(self):
        """Special case: unified-agent should get ALL tools."""
        config = _make_config(agent_id="unified-agent", tool_ids=[])
        agent = _make_agent(config)
        tools = agent.tool_definitions
        # Should return full tool list, not empty
        assert len(tools) > 0


class TestSpecializedAgentModelPreference:
    def test_model_preference_overrides_default(self):
        config = _make_config(model_preference="claude-haiku-4-5-20251001")
        agent = _make_agent(config)
        assert agent.model_preference == "claude-haiku-4-5-20251001"

    def test_model_preference_none_inherits(self):
        config = _make_config(model_preference=None)
        agent = _make_agent(config)
        assert agent.model_preference is None


class TestSpecializedAgentProtocol:
    def test_conforms_to_agent_protocol(self):
        agent = _make_agent()
        # AgentProtocol is @runtime_checkable
        assert isinstance(agent, AgentProtocol)

    def test_has_all_protocol_properties(self):
        agent = _make_agent()
        # All protocol properties should be accessible
        assert agent.agent_id == "test-agent"
        assert agent.display_name == "Test Agent"
        assert agent.description == "A test agent"
        assert isinstance(agent.routing_rules, list)
        assert isinstance(agent.tool_ids, list)
        assert isinstance(agent.rag_partitions, list)
        assert isinstance(agent.system_prompt, str)
        assert isinstance(agent.max_steps, int)
        assert agent.requires_confirmation is False
