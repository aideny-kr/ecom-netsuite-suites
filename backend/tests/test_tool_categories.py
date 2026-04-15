# backend/tests/test_tool_categories.py
"""Tool category derivation used by orchestrator routing + confidence scoring.

Replaces the four parallel frozensets (_FINANCIAL_TOOLS, _DATA_TABLE_TOOLS,
_BIGQUERY_TOOLS in orchestrator.py; data_tools in base_agent.py) with a
single lookup so a new tool only needs a category declared in tools.py.
"""

from app.services.chat.tool_categories import categorize


class TestCategorize:
    def test_netsuite_suiteql_is_data_table(self):
        assert categorize("netsuite_suiteql") == "data_table"

    def test_netsuite_financial_report_is_financial(self):
        assert categorize("netsuite_financial_report") == "financial"

    def test_bigquery_sql_is_bigquery(self):
        assert categorize("bigquery_sql") == "bigquery"

    def test_pivot_query_result_is_data_table(self):
        assert categorize("pivot_query_result") == "data_table"

    def test_rag_search_is_rag(self):
        assert categorize("rag_search") == "rag"

    def test_workspace_read_file_is_workspace(self):
        assert categorize("workspace_read_file") == "workspace"

    def test_external_mcp_run_report_is_financial(self):
        # Oracle NetSuite MCP exposes ns_runReport via the ext__ namespace.
        assert categorize("ext__ns_runReport__abcd1234") == "financial"

    def test_external_mcp_suiteql_is_data_table(self):
        assert categorize("ext__ns_runCustomSuiteQL__abcd1234") == "data_table"

    def test_unknown_tool_is_other(self):
        assert categorize("some_new_tool") == "other"

    def test_dotted_names_normalized(self):
        # Tool registry uses dotted names; LLM sees underscores. Both map equally.
        assert categorize("netsuite.suiteql") == "data_table"
        assert categorize("bigquery.sql") == "bigquery"


class TestOrchestratorCategoryCheckers:
    """Prove the orchestrator's legacy helpers are now category-driven."""

    def test_is_financial_tool_uses_categorize(self):
        from app.services.chat.orchestrator import _is_financial_tool

        assert _is_financial_tool("netsuite_financial_report") is True
        assert _is_financial_tool("netsuite_suiteql") is False
        assert _is_financial_tool("ext__connector1__ns_runReport") is True

    def test_is_data_table_tool_uses_categorize(self):
        from app.services.chat.orchestrator import _is_data_table_tool

        assert _is_data_table_tool("netsuite_suiteql") is True
        assert _is_data_table_tool("bigquery_sql") is True
        assert _is_data_table_tool("pivot_query_result") is True
        assert _is_data_table_tool("rag_search") is False

    def test_no_hardcoded_financial_tools_frozenset(self):
        from app.services.chat import orchestrator

        assert not hasattr(orchestrator, "_FINANCIAL_TOOLS"), (
            "Delete _FINANCIAL_TOOLS frozenset; use categorize() instead."
        )
        assert not hasattr(orchestrator, "_DATA_TABLE_TOOLS"), (
            "Delete _DATA_TABLE_TOOLS frozenset; use categorize() instead."
        )
        assert not hasattr(orchestrator, "_BIGQUERY_TOOLS"), (
            "Delete _BIGQUERY_TOOLS frozenset; use categorize() instead."
        )


class TestBaseAgentConfidenceCategoryCheck:
    def test_data_tool_set_not_hardcoded(self):
        import inspect

        from app.services.chat.agents import base_agent

        source = inspect.getsource(base_agent)
        assert '"netsuite_suiteql"' not in source or "categorize" in source, (
            "base_agent must use categorize() instead of hardcoded data tool set."
        )
        # Specifically: the old set used to be on one line. Catch its return.
        assert 'data_tools = {"netsuite_suiteql"' not in source and "data_tools = {'netsuite_suiteql'" not in source, (
            "data_tools hardcoded set must be removed; use categorize() instead."
        )
