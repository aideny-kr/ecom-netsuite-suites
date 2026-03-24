"""Tests for BI agent YAML config and prompt file."""

import re
from pathlib import Path

from app.services.chat.agents.agent_yaml_config import AgentYAMLConfig

CONFIGS_DIR = Path(__file__).resolve().parent.parent / "app" / "services" / "chat" / "agents" / "configs"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "app" / "services" / "chat" / "agents" / "prompts"


def _load_bi_config() -> AgentYAMLConfig:
    return AgentYAMLConfig.from_yaml(CONFIGS_DIR / "bi_agent.yaml")


def _read_bi_prompt() -> str:
    config = _load_bi_config()
    return (PROMPTS_DIR / config.prompt_file).read_text()


class TestBiAgentConfig:
    def test_bi_agent_yaml_parses(self):
        config = _load_bi_config()
        assert config is not None

    def test_bi_agent_id(self):
        assert _load_bi_config().agent_id == "bi-agent"

    def test_bi_agent_tools(self):
        tool_ids = _load_bi_config().tool_ids
        assert "bigquery_sql" in tool_ids
        assert "bigquery_schema" in tool_ids
        assert "bigquery_cost_estimate" in tool_ids
        assert "netsuite_pivot_query_result" in tool_ids
        assert "rag_search" in tool_ids

    def test_bi_agent_rag_partitions(self):
        parts = _load_bi_config().rag_partitions
        assert "bi/schema-docs" in parts
        assert "bi/common-queries" in parts
        assert "bi/metric-definitions" in parts

    def test_bi_agent_routing_rules_compile(self):
        for rule in _load_bi_config().routing_rules:
            re.compile(rule.pattern)  # Should not raise

    def test_bi_agent_routes_revenue_query(self):
        rules = _load_bi_config().routing_rules
        query = "What's our revenue by region?"
        assert any(re.search(r.pattern, query) for r in rules)

    def test_bi_agent_routes_chart_query(self):
        rules = _load_bi_config().routing_rules
        query = "Show me a chart of monthly sales"
        assert any(re.search(r.pattern, query) for r in rules)

    def test_bi_agent_routes_bigquery_query(self):
        rules = _load_bi_config().routing_rules
        query = "Run a BigQuery query to find top customers"
        assert any(re.search(r.pattern, query) for r in rules)

    def test_bi_agent_does_not_route_netsuite(self):
        rules = _load_bi_config().routing_rules
        query = "What's the status of SO-12345?"
        assert not any(re.search(r.pattern, query) for r in rules)

    def test_bi_agent_does_not_route_greeting(self):
        rules = _load_bi_config().routing_rules
        query = "Hello, how are you?"
        assert not any(re.search(r.pattern, query) for r in rules)

    def test_bi_agent_prompt_file_exists(self):
        config = _load_bi_config()
        assert config.prompt_file is not None
        assert (PROMPTS_DIR / config.prompt_file).exists()

    def test_bi_agent_max_steps(self):
        assert _load_bi_config().max_steps == 10

    def test_bi_agent_read_only(self):
        assert _load_bi_config().requires_confirmation is False


class TestBiAgentPrompt:
    def test_has_bigquery_dialect(self):
        prompt = _read_bi_prompt()
        assert "Standard SQL" in prompt
        assert "backtick" in prompt.lower()

    def test_has_chart_heuristic(self):
        prompt = _read_bi_prompt()
        assert "Line chart" in prompt or "line chart" in prompt
        assert "Bar chart" in prompt or "bar chart" in prompt

    def test_has_workflow_steps(self):
        prompt = _read_bi_prompt()
        assert "bigquery_schema" in prompt
        assert "bigquery_sql" in prompt

    def test_has_safe_divide(self):
        prompt = _read_bi_prompt()
        assert "SAFE_DIVIDE" in prompt

    def test_no_suiteql_rules(self):
        prompt = _read_bi_prompt()
        assert "FETCH FIRST" not in prompt
