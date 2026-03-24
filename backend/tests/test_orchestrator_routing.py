"""Tests for _select_agent() orchestrator integration."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSelectAgent:

    @pytest.mark.asyncio
    async def test_no_configs_returns_unified(self):
        """When no YAML configs are loaded, _select_agent returns None (use UnifiedAgent)."""
        from app.services.chat.orchestrator import _select_agent

        result = await _select_agent(
            query="test query",
            tenant_id=uuid.uuid4(),
            db=AsyncMock(),
            adapter=AsyncMock(),
        )
        # None means "use UnifiedAgent" (no specialized agent available)
        assert result is None

    @pytest.mark.asyncio
    async def test_tier1_match_returns_agent_id(self):
        """When RuleRouter matches, return that agent_id."""
        from app.services.chat.orchestrator import _select_agent

        with patch("app.services.chat.orchestrator._agent_registry") as mock_registry:
            # Setup: registry has configs, rule router matches
            mock_registry.configs = {"pricing-agent": MagicMock()}
            mock_registry.get_enabled_agents = AsyncMock(return_value=[
                MagicMock(agent_id="pricing-agent", routing_rules=[MagicMock(pattern="(?i)price", priority=0)]),
            ])
            mock_registry.is_healthy = MagicMock(return_value=True)

            with patch("app.services.chat.orchestrator.RuleRouter") as MockRouter:
                MockRouter.return_value.route.return_value = "pricing-agent"

                result = await _select_agent(
                    query="what's the price",
                    tenant_id=uuid.uuid4(),
                    db=AsyncMock(),
                    adapter=AsyncMock(),
                )
                assert result == "pricing-agent"

    @pytest.mark.asyncio
    async def test_tier1_none_falls_to_tier2(self):
        """When RuleRouter returns None but SemanticRouter matches, return that agent_id."""
        from app.services.chat.orchestrator import _select_agent

        with patch("app.services.chat.orchestrator._agent_registry") as mock_registry:
            mock_registry.configs = {"pricing-agent": MagicMock()}
            mock_registry.get_enabled_agents = AsyncMock(return_value=[
                MagicMock(agent_id="pricing-agent"),
            ])
            mock_registry.is_healthy = MagicMock(return_value=True)

            with patch("app.services.chat.orchestrator.RuleRouter") as MockRuleRouter, \
                 patch("app.services.chat.orchestrator.SemanticRouter") as MockSemRouter:
                MockRuleRouter.return_value.route.return_value = None
                MockSemRouter.return_value.route = AsyncMock(return_value="pricing-agent")

                result = await _select_agent(
                    query="how much does this cost",
                    tenant_id=uuid.uuid4(),
                    db=AsyncMock(),
                    adapter=AsyncMock(),
                )
                assert result == "pricing-agent"

    @pytest.mark.asyncio
    async def test_tier2_unified_returns_none(self):
        """When SemanticRouter returns 'unified-agent', _select_agent returns None."""
        from app.services.chat.orchestrator import _select_agent

        with patch("app.services.chat.orchestrator._agent_registry") as mock_registry:
            mock_registry.configs = {"pricing-agent": MagicMock()}
            mock_registry.get_enabled_agents = AsyncMock(return_value=[
                MagicMock(agent_id="pricing-agent"),
            ])

            with patch("app.services.chat.orchestrator.RuleRouter") as MockRuleRouter, \
                 patch("app.services.chat.orchestrator.SemanticRouter") as MockSemRouter:
                MockRuleRouter.return_value.route.return_value = None
                MockSemRouter.return_value.route = AsyncMock(return_value="unified-agent")

                result = await _select_agent(
                    query="hello",
                    tenant_id=uuid.uuid4(),
                    db=AsyncMock(),
                    adapter=AsyncMock(),
                )
                assert result is None

    @pytest.mark.asyncio
    async def test_unhealthy_agent_skipped(self):
        """When matched agent is unhealthy (circuit breaker), skip it."""
        from app.services.chat.orchestrator import _select_agent

        with patch("app.services.chat.orchestrator._agent_registry") as mock_registry:
            mock_registry.configs = {"pricing-agent": MagicMock()}
            mock_registry.get_enabled_agents = AsyncMock(return_value=[
                MagicMock(agent_id="pricing-agent"),
            ])
            mock_registry.is_healthy = MagicMock(return_value=False)

            with patch("app.services.chat.orchestrator.RuleRouter") as MockRuleRouter:
                MockRuleRouter.return_value.route.return_value = "pricing-agent"

                result = await _select_agent(
                    query="price check",
                    tenant_id=uuid.uuid4(),
                    db=AsyncMock(),
                    adapter=AsyncMock(),
                )
                # Unhealthy → skip, return None for UnifiedAgent fallback
                assert result is None
