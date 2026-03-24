"""End-to-end integration tests for the BI agent pipeline."""

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chat.agents.agent_registry import AgentRegistry
from app.services.chat.agents.specialized_agent import SpecializedAgent
from app.services.chat.routing.rule_router import RuleRouter
from app.services.chat.chart_extractor import extract_charts

CONFIGS_DIR = (
    Path(__file__).resolve().parent.parent
    / "app"
    / "services"
    / "chat"
    / "agents"
    / "configs"
)

SAMPLE_CHART_RESPONSE = '''Revenue has grown 23% QoQ.

<chart>{"chart_type": "bar", "title": "Revenue by Region", "x_axis": {"label": "Region", "key": "region"}, "y_axes": [{"label": "Revenue ($)", "key": "revenue"}], "data": [{"region": "US", "revenue": 1200000}, {"region": "EU", "revenue": 800000}]}</chart>

The strongest growth was in the US market.'''


def _make_registry() -> AgentRegistry:
    registry = AgentRegistry()
    registry.load_configs(CONFIGS_DIR)
    return registry


class TestBiAgentRouting:

    def test_revenue_query_routes_to_bi_agent(self):
        registry = _make_registry()
        configs = list(registry.configs.values())
        router = RuleRouter([(c, True) for c in configs])
        result = router.route("What's our revenue by region?")
        assert result == "bi-agent"

    def test_netsuite_query_does_not_route_to_bi(self):
        registry = _make_registry()
        configs = list(registry.configs.values())
        router = RuleRouter([(c, True) for c in configs])
        result = router.route("What's the status of SO-12345?")
        assert result != "bi-agent"

    def test_backward_compat_pricing_query(self):
        registry = _make_registry()
        configs = list(registry.configs.values())
        router = RuleRouter([(c, True) for c in configs])
        # "markup" should match pricing-agent, not bi-agent
        result = router.route("What's the markup on SKU-1234?")
        assert result == "pricing-agent"

    def test_backward_compat_netsuite_unchanged(self):
        registry = _make_registry()
        configs = list(registry.configs.values())
        router = RuleRouter([(c, True) for c in configs])
        result = router.route("Show me all open POs")
        assert result is None  # Falls to UnifiedAgent

    def test_chart_query_routes_to_bi(self):
        registry = _make_registry()
        configs = list(registry.configs.values())
        router = RuleRouter([(c, True) for c in configs])
        result = router.route("Show me a chart of monthly sales")
        assert result == "bi-agent"

    def test_bigquery_explicit_routes_to_bi(self):
        registry = _make_registry()
        configs = list(registry.configs.values())
        router = RuleRouter([(c, True) for c in configs])
        result = router.route("Run a BigQuery query to find top customers")
        assert result == "bi-agent"


class TestBiAgentInstantiation:

    def test_bi_agent_uses_bigquery_tools(self):
        registry = _make_registry()
        agent = registry.instantiate(
            agent_id="bi-agent",
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
        )
        tool_names = {t["name"] for t in agent.tool_definitions}
        assert "bigquery_sql" in tool_names
        assert "bigquery_schema" in tool_names
        assert "bigquery_cost_estimate" in tool_names
        # Should NOT have NetSuite-specific tools
        assert "netsuite_suiteql" not in tool_names

    def test_bi_agent_prompt_loaded(self):
        registry = _make_registry()
        agent = registry.instantiate(
            agent_id="bi-agent",
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
        )
        prompt = agent.system_prompt
        assert "Standard SQL" in prompt
        assert "SAFE_DIVIDE" in prompt
        assert "FETCH FIRST" not in prompt

    def test_bi_agent_schema_knowledge_injected(self):
        registry = _make_registry()
        agent = registry.instantiate(
            agent_id="bi-agent",
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
            knowledge=["Table: analytics.orders\nColumns:\n  - order_id (STRING)"],
        )
        prompt = agent.system_prompt
        assert "<knowledge>" in prompt
        assert "analytics.orders" in prompt

    def test_bi_agent_properties_from_yaml(self):
        registry = _make_registry()
        agent = registry.instantiate(
            agent_id="bi-agent",
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
        )
        assert agent.display_name == "BI Analyst"
        assert agent.max_steps == 10
        assert agent.cost_budget == 0.50
        assert agent.requires_confirmation is False
        assert "bi/schema-docs" in agent.rag_partitions


class TestChartPipeline:

    def test_chart_extracted_from_response(self):
        cleaned, charts = extract_charts(SAMPLE_CHART_RESPONSE)
        assert len(charts) == 1
        assert charts[0].chart_type == "bar"
        assert charts[0].title == "Revenue by Region"

    def test_chart_stripped_from_text(self):
        cleaned, charts = extract_charts(SAMPLE_CHART_RESPONSE)
        assert "<chart>" not in cleaned
        assert "Revenue has grown" in cleaned
        assert "strongest growth" in cleaned

    def test_no_chart_passthrough(self):
        text = "Revenue is $1.2M this quarter."
        cleaned, charts = extract_charts(text)
        assert cleaned == text
        assert charts == []

    def test_multiple_charts_extracted(self):
        text = (
            'First chart: <chart>{"chart_type": "bar", "title": "A", '
            '"x_axis": {"label": "X", "key": "x"}, "y_axes": [{"label": "Y", "key": "y"}], '
            '"data": [{"x": 1, "y": 2}]}</chart> '
            'Second chart: <chart>{"chart_type": "line", "title": "B", '
            '"x_axis": {"label": "X", "key": "x"}, "y_axes": [{"label": "Y", "key": "y"}], '
            '"data": [{"x": 1, "y": 2}]}</chart>'
        )
        cleaned, charts = extract_charts(text)
        assert len(charts) == 2
        assert charts[0].chart_type == "bar"
        assert charts[1].chart_type == "line"

    def test_malformed_chart_json_skipped(self):
        text = 'Some text <chart>{invalid json}</chart> more text'
        cleaned, charts = extract_charts(text)
        assert len(charts) == 0
        assert "Some text" in cleaned

    def test_invalid_chart_type_defaults_to_bar(self):
        text = (
            '<chart>{"chart_type": "radar", "title": "Test", '
            '"x_axis": {"label": "X", "key": "x"}, '
            '"y_axes": [{"label": "Y", "key": "y"}], '
            '"data": [{"x": 1, "y": 2}]}</chart>'
        )
        cleaned, charts = extract_charts(text)
        assert len(charts) == 1
        assert charts[0].chart_type == "bar"


class TestBigQueryToolGuardrails:

    @pytest.mark.asyncio
    async def test_read_only_enforced(self):
        from app.services.bigquery_service import execute_query

        with pytest.raises(ValueError, match="[Rr]ead.only"):
            await execute_query({"type": "sa"}, "p", "INSERT INTO t VALUES (1)")

    @pytest.mark.asyncio
    async def test_read_only_rejects_delete(self):
        from app.services.bigquery_service import execute_query

        with pytest.raises(ValueError, match="[Rr]ead.only"):
            await execute_query({"type": "sa"}, "p", "DELETE FROM t WHERE id = 1")

    @pytest.mark.asyncio
    async def test_read_only_rejects_drop(self):
        from app.services.bigquery_service import execute_query

        with pytest.raises(ValueError, match="[Rr]ead.only"):
            await execute_query({"type": "sa"}, "p", "DROP TABLE t")

    @pytest.mark.asyncio
    async def test_no_connector_returns_error(self):
        from app.mcp.tools.bigquery_tools import bigquery_sql_execute

        ctx = {"tenant_id": str(uuid.uuid4()), "db": AsyncMock()}
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        ctx["db"].execute = AsyncMock(return_value=mock_result)
        result = await bigquery_sql_execute({"query": "SELECT 1"}, ctx)
        assert result.get("error") is True


class TestSelectAgentIntegration:

    @pytest.mark.asyncio
    async def test_select_agent_bi_query(self):
        from app.services.chat.orchestrator import _agent_registry, _select_agent

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
            )
            assert result == "bi-agent"
        finally:
            _agent_registry.configs.clear()

    @pytest.mark.asyncio
    async def test_select_agent_bi_chart_query(self):
        from app.services.chat.orchestrator import _agent_registry, _select_agent

        _agent_registry.load_configs(CONFIGS_DIR)
        try:
            mock_db = AsyncMock()
            mock_result = MagicMock()
            mock_result.all.return_value = []
            mock_db.execute = AsyncMock(return_value=mock_result)

            result = await _select_agent(
                query="Show me a dashboard of monthly KPIs",
                tenant_id=uuid.uuid4(),
                db=mock_db,
                adapter=AsyncMock(),
            )
            assert result == "bi-agent"
        finally:
            _agent_registry.configs.clear()

    @pytest.mark.asyncio
    async def test_no_configs_returns_none(self):
        from app.services.chat.orchestrator import _agent_registry, _select_agent

        _agent_registry.configs.clear()
        result = await _select_agent(
            query="revenue by region",
            tenant_id=uuid.uuid4(),
            db=AsyncMock(),
            adapter=AsyncMock(),
        )
        assert result is None
