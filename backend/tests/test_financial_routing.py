"""Tests that financial report queries route to unified-agent, not bi-agent.

Financial statements (income statement, P&L, balance sheet, trial balance)
must use NetSuite's netsuite_financial_report tool via the UnifiedAgent path.
The BI agent handles BigQuery analytics only.
"""

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chat.agents.agent_yaml_config import AgentYAMLConfig
from app.services.chat.routing.rule_router import RuleRouter

CONFIGS_DIR = Path(__file__).resolve().parent.parent / "app" / "services" / "chat" / "agents" / "configs"


def _load_configs() -> list[AgentYAMLConfig]:
    configs = []
    for f in sorted(CONFIGS_DIR.glob("*.yaml")):
        configs.append(AgentYAMLConfig.from_yaml(f))
    return configs


class TestFinancialReportsRouteToUnified:
    """Financial report queries must NOT route to bi-agent."""

    @pytest.mark.parametrize("query", [
        "Give me the income statement for the last 4 months",
        "Show me the P&L report",
        "Balance sheet as of March 2026",
        "What's the profit and loss for Q1?",
        "Trial balance for February 2026",
        "Show me the financial statement for this quarter",
        "Cash flow statement for 2026",
        "P&L by subsidiary",
    ])
    def test_pure_financial_does_not_route_to_bi_agent(self, query):
        """Financial queries without BI keywords don't match BI agent patterns."""
        configs = _load_configs()
        router = RuleRouter([(c, True) for c in configs])
        result = router.route(query)
        assert result != "bi-agent", (
            f"Financial query '{query}' routed to bi-agent — should be unified-agent"
        )

    @pytest.mark.parametrize("query", [
        "Run the income statement with chart",
        "Compare income statement month over month",
    ])
    @pytest.mark.asyncio
    async def test_mixed_financial_bi_keywords_still_use_unified(self, query):
        """Queries with both financial AND BI keywords are financial-first.
        RuleRouter might match BI, but _select_agent pre-filter forces unified."""
        from app.services.chat.orchestrator import _select_agent, _agent_registry

        _agent_registry.load_configs(CONFIGS_DIR)
        try:
            mock_db = AsyncMock()
            mock_result = MagicMock()
            mock_result.all.return_value = []
            mock_db.execute = AsyncMock(return_value=mock_result)

            result = await _select_agent(
                query=query,
                tenant_id=uuid.uuid4(),
                db=mock_db,
                adapter=AsyncMock(),
                is_financial=True,
            )
            assert result is None, f"Financial query '{query}' should bypass routing"
        finally:
            _agent_registry.configs.clear()

    @pytest.mark.parametrize("query", [
        "What's our revenue by region this quarter?",
        "Show me monthly sales trends",
        "Top 10 customers by lifetime value",
        "Run a BigQuery query on sales data",
    ])
    def test_bi_queries_still_route_to_bi_agent(self, query):
        configs = _load_configs()
        router = RuleRouter([(c, True) for c in configs])
        result = router.route(query)
        assert result == "bi-agent", (
            f"BI query '{query}' should route to bi-agent, got '{result}'"
        )


class TestSelectAgentFinancialPreFilter:
    """_select_agent should bypass routing for financial queries."""

    @pytest.mark.asyncio
    async def test_financial_query_bypasses_routing(self):
        from app.services.chat.orchestrator import _select_agent, _agent_registry

        _agent_registry.load_configs(CONFIGS_DIR)
        try:
            mock_db = AsyncMock()
            mock_result = MagicMock()
            mock_result.all.return_value = []
            mock_db.execute = AsyncMock(return_value=mock_result)

            # "income statement" is financial — should return None (unified-agent)
            result = await _select_agent(
                query="Give me the income statement for the last 4 months",
                tenant_id=uuid.uuid4(),
                db=mock_db,
                adapter=AsyncMock(),
                is_financial=True,
            )
            assert result is None, "Financial query should bypass routing → None (unified-agent)"
        finally:
            _agent_registry.configs.clear()

    @pytest.mark.asyncio
    async def test_non_financial_query_routes_normally(self):
        from app.services.chat.orchestrator import _select_agent, _agent_registry

        _agent_registry.load_configs(CONFIGS_DIR)
        try:
            mock_db = AsyncMock()
            mock_result = MagicMock()
            mock_result.all.return_value = []
            mock_db.execute = AsyncMock(return_value=mock_result)

            result = await _select_agent(
                query="What's our revenue by region?",
                tenant_id=uuid.uuid4(),
                db=mock_db,
                adapter=AsyncMock(),
                is_financial=False,
            )
            assert result == "bi-agent"
        finally:
            _agent_registry.configs.clear()


class TestBiAgentDescriptionExcludesFinancials:
    """BI agent description should clarify it doesn't handle financial statements."""

    def test_bi_agent_description_excludes_financials(self):
        configs = _load_configs()
        bi_config = next(c for c in configs if c.agent_id == "bi-agent")
        desc = bi_config.description.lower()
        assert "does not" in desc or "not handle" in desc.lower(), (
            "BI agent description should clarify it doesn't handle financial statements"
        )
