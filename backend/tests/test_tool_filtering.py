"""Tests for per-agent tool filtering."""

from app.services.chat.agents.tool_filter import get_tools_for_agent

# Sample tool definitions matching Anthropic format
SAMPLE_TOOLS = [
    {"name": "netsuite_suiteql", "description": "Run SuiteQL", "input_schema": {}},
    {"name": "rag_search", "description": "Search docs", "input_schema": {}},
    {"name": "web_search", "description": "Web search", "input_schema": {}},
    {"name": "workspace_read_file", "description": "Read file", "input_schema": {}},
    {"name": "netsuite_get_metadata", "description": "Get metadata", "input_schema": {}},
]


class TestToolFiltering:

    def test_get_tools_for_agent_filters_correctly(self):
        result = get_tools_for_agent(
            all_tools=SAMPLE_TOOLS,
            tool_ids=["netsuite_suiteql", "rag_search"],
        )
        names = {t["name"] for t in result}
        assert names == {"netsuite_suiteql", "rag_search"}

    def test_get_tools_for_agent_returns_all_when_none(self):
        """tool_ids=None means no filtering — return all."""
        result = get_tools_for_agent(
            all_tools=SAMPLE_TOOLS,
            tool_ids=None,
        )
        assert len(result) == len(SAMPLE_TOOLS)

    def test_get_tools_for_agent_empty_list_returns_none(self):
        result = get_tools_for_agent(
            all_tools=SAMPLE_TOOLS,
            tool_ids=[],
        )
        assert result == []

    def test_get_tools_preserves_full_definition(self):
        """Filtered tools should retain their full definition dict."""
        result = get_tools_for_agent(
            all_tools=SAMPLE_TOOLS,
            tool_ids=["rag_search"],
        )
        assert len(result) == 1
        assert result[0]["name"] == "rag_search"
        assert result[0]["description"] == "Search docs"
