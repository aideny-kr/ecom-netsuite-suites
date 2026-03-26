"""End-to-end integration tests for the agent framework.

Uses real routing, registry, config loading, and agent instantiation.
Mocks only LLM adapter and DB operations.
"""

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chat.agents.agent_registry import AgentRegistry
from app.services.chat.agents.specialized_agent import SpecializedAgent
from app.services.chat.routing.rule_router import RuleRouter

CONFIGS_DIR = Path(__file__).resolve().parent.parent / "app" / "services" / "chat" / "agents" / "configs"


def _make_registry() -> AgentRegistry:
    """Load real YAML configs from the configs directory."""
    registry = AgentRegistry()
    registry.load_configs(CONFIGS_DIR)
    return registry


class TestPricingRouting:
    def test_pricing_query_routes_to_pricing_agent(self):
        """Real RuleRouter + real pricing config routes pricing queries correctly."""
        registry = _make_registry()
        configs = list(registry.configs.values())
        router = RuleRouter([(c, True) for c in configs])
        result = router.route("What's the margin on SKU-1234?")
        assert result == "pricing-agent"

    def test_generic_query_routes_to_none(self):
        """Generic queries don't match any specialized agent."""
        registry = _make_registry()
        configs = list(registry.configs.values())
        router = RuleRouter([(c, True) for c in configs])
        result = router.route("Hello, how are you?")
        assert result is None  # Falls to UnifiedAgent

    def test_investigation_query_routes_to_none(self):
        """Investigation queries don't match pricing agent."""
        registry = _make_registry()
        configs = list(registry.configs.values())
        router = RuleRouter([(c, True) for c in configs])
        result = router.route("What happened with SO-12345 last week?")
        assert result is None  # No investigation specialist yet

    def test_multiple_pricing_keywords_still_single_match(self):
        """Query with multiple pricing keywords still resolves to single agent."""
        registry = _make_registry()
        configs = list(registry.configs.values())
        router = RuleRouter([(c, True) for c in configs])
        result = router.route("Show me pricing and margin for this tariff item")
        assert result == "pricing-agent"

    def test_disabled_agent_not_routed(self):
        """Disabled agent is excluded from routing."""
        registry = _make_registry()
        configs = list(registry.configs.values())
        # Mark all agents as disabled
        router = RuleRouter([(c, False) for c in configs])
        result = router.route("What's the margin on SKU-1234?")
        assert result is None


class TestPricingAgentInstantiation:
    def test_pricing_agent_instantiation(self):
        registry = _make_registry()
        agent = registry.instantiate(
            agent_id="pricing-agent",
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
        )
        assert isinstance(agent, SpecializedAgent)
        assert agent.agent_name == "pricing-agent"

    def test_pricing_agent_uses_filtered_tools(self):
        registry = _make_registry()
        agent = registry.instantiate(
            agent_id="pricing-agent",
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
        )
        tool_names = {t["name"] for t in agent.tool_definitions}
        # Pricing agent YAML specifies: netsuite_suiteql, netsuite_get_record,
        # rag_search, pivot_query_result
        # Only tools that exist in ALLOWED_CHAT_TOOLS will appear
        allowed_pricing_tools = {
            "netsuite_suiteql",
            "netsuite_get_record",
            "rag_search",
            "pivot_query_result",
        }
        assert tool_names <= allowed_pricing_tools
        # Should NOT have tools outside the pricing agent's tool_ids
        assert "web_search" not in tool_names

    def test_pricing_agent_prompt_loaded(self):
        registry = _make_registry()
        agent = registry.instantiate(
            agent_id="pricing-agent",
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
        )
        prompt = agent.system_prompt
        # Prompt file should be loaded and non-empty
        assert len(prompt) > 0

    def test_pricing_agent_prompt_has_knowledge_when_provided(self):
        registry = _make_registry()
        agent = registry.instantiate(
            agent_id="pricing-agent",
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
            knowledge=["Pricing chunk 1", "Pricing chunk 2"],
        )
        prompt = agent.system_prompt
        assert "<knowledge>" in prompt
        assert "Pricing chunk 1" in prompt
        assert "Pricing chunk 2" in prompt

    def test_pricing_agent_no_knowledge_without_chunks(self):
        registry = _make_registry()
        agent = registry.instantiate(
            agent_id="pricing-agent",
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
        )
        prompt = agent.system_prompt
        assert "<knowledge>" not in prompt

    def test_pricing_agent_properties_from_yaml(self):
        registry = _make_registry()
        agent = registry.instantiate(
            agent_id="pricing-agent",
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
        )
        assert agent.display_name == "Pricing Specialist"
        assert agent.max_steps == 8
        assert agent.cost_budget == 0.50
        assert agent.requires_confirmation is False
        assert "pricing/margin-rules" in agent.rag_partitions

    def test_instantiate_nonexistent_agent_raises_key_error(self):
        registry = _make_registry()
        with pytest.raises(KeyError):
            registry.instantiate(
                agent_id="nonexistent-agent",
                tenant_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                correlation_id="test",
            )


class TestSelectAgentIntegration:
    @pytest.mark.asyncio
    async def test_select_agent_pricing_query(self):
        from app.services.chat.orchestrator import _agent_registry, _select_agent

        # Load real configs into the module-level registry
        _agent_registry.load_configs(CONFIGS_DIR)
        try:
            mock_db = AsyncMock()
            mock_result = MagicMock()
            mock_result.all.return_value = []  # No DB overrides
            mock_db.execute = AsyncMock(return_value=mock_result)

            result = await _select_agent(
                query="What's the price margin on this item?",
                tenant_id=uuid.uuid4(),
                db=mock_db,
                adapter=AsyncMock(),
            )
            assert result == "pricing-agent"
        finally:
            # Restore empty state
            _agent_registry.configs.clear()

    @pytest.mark.asyncio
    async def test_select_agent_generic_returns_none(self):
        from app.services.chat.orchestrator import _agent_registry, _select_agent

        _agent_registry.load_configs(CONFIGS_DIR)
        try:
            mock_db = AsyncMock()
            mock_result = MagicMock()
            mock_result.all.return_value = []
            mock_db.execute = AsyncMock(return_value=mock_result)

            # Mock semantic router to return unified
            with patch("app.services.chat.orchestrator.SemanticRouter") as MockSem:
                MockSem.return_value.route = AsyncMock(return_value="unified-agent")
                result = await _select_agent(
                    query="Hello, how are you?",
                    tenant_id=uuid.uuid4(),
                    db=mock_db,
                    adapter=AsyncMock(),
                )
            assert result is None
        finally:
            _agent_registry.configs.clear()

    @pytest.mark.asyncio
    async def test_no_configs_returns_none(self):
        from app.services.chat.orchestrator import _agent_registry, _select_agent

        # Ensure no configs loaded
        _agent_registry.configs.clear()
        result = await _select_agent(
            query="anything",
            tenant_id=uuid.uuid4(),
            db=AsyncMock(),
            adapter=AsyncMock(),
        )
        assert result is None


class TestBackwardCompatibility:
    def test_no_configs_dir_registry_empty(self):
        registry = AgentRegistry()
        registry.load_configs(Path("/nonexistent/path"))
        assert len(registry.configs) == 0

    def test_unified_agent_config_has_no_routing_rules(self):
        registry = _make_registry()
        unified = registry.configs.get("unified-agent")
        assert unified is not None
        assert unified.routing_rules == []

    def test_circuit_breaker_healthy(self):
        registry = AgentRegistry()
        assert registry.is_healthy(error_count=2, success_count=98) is True

    def test_circuit_breaker_unhealthy(self):
        registry = AgentRegistry()
        assert registry.is_healthy(error_count=6, success_count=94) is False

    def test_circuit_breaker_no_data_is_healthy(self):
        registry = AgentRegistry()
        assert registry.is_healthy(error_count=0, success_count=0) is True

    def test_circuit_breaker_boundary_exactly_five_percent(self):
        registry = AgentRegistry()
        # 5/100 = exactly 5%, NOT > 5%, so should be healthy
        assert registry.is_healthy(error_count=5, success_count=95) is True

    def test_registry_loads_all_yaml_configs(self):
        registry = _make_registry()
        # Should have at least unified-agent and pricing-agent
        assert "unified-agent" in registry.configs
        assert "pricing-agent" in registry.configs

    def test_agent_overrides_merge(self):
        registry = _make_registry()
        agent = registry.instantiate(
            agent_id="pricing-agent",
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
            overrides={"max_steps": 12},
        )
        assert agent.max_steps == 12
