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
