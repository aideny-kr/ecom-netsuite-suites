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

    def test_external_mcp_celigo_list_tool_is_data_table(self):
        # Celigo's hosted MCP server ships list_* read tools (list_flow_errors,
        # list_flows, ...). Without this, categorize() falls through to "other",
        # _is_data_table_tool returns False, _intercept_tool_result never runs,
        # and the LLM restates raw counts from the tool's JSON in prose --
        # exactly what SSE interception exists to prevent (feedback_no_llm_numbers).
        connector_id = "ab" * 16  # 32 hex chars, matches _make_ext_tool_name's format
        assert categorize(f"ext__{connector_id}__list_flow_errors") == "data_table"

    def test_external_mcp_celigo_write_tool_is_mutation_not_data_table(self):
        # Write verbs must stay classified as "mutation" (HITL path) even
        # though they live in the same Celigo tool namespace as the reads.
        connector_id = "ab" * 16
        assert categorize(f"ext__{connector_id}__upsert_flow") == "mutation"

    def test_external_mcp_celigo_unrecognized_tool_is_other(self):
        # Fails closed: a name that isn't in Celigo's enumerated read catalog
        # (celigo_tool_policy._READ_TOOLS) must not be guessed into data_table.
        connector_id = "ab" * 16
        assert categorize(f"ext__{connector_id}__some_unknown_celigo_tool") == "other"

    def test_unknown_tool_is_other(self):
        assert categorize("some_new_tool") == "other"

    def test_dotted_names_normalized(self):
        # Tool registry uses dotted names; LLM sees underscores. Both map equally.
        assert categorize("netsuite.suiteql") == "data_table"
        assert categorize("bigquery.sql") == "bigquery"

    def test_local_celigo_tools_are_data_table_both_spellings(self):
        # Task 3 (spec docs/superpowers/specs/2026-09-04-celigo-chat-access.md §6):
        # local celigo.* chat tools carry row data over a synced snapshot, same
        # interception need as netsuite_suiteql/bigquery_sql above.
        for dotted in ("celigo.integrations", "celigo.flows", "celigo.flow_steps", "celigo.flow_errors"):
            assert categorize(dotted) == "data_table"
            assert categorize(dotted.replace(".", "_")) == "data_table"


class TestIsCeligoSource:
    """`is_celigo_source` -- the WIDER "is this any Celigo tool" question
    `_compute_source_pin_update` needs, vs. `is_celigo_tool`'s narrower
    "is this the external hosted-API read tool" (task 3)."""

    def test_local_dotted_and_underscored_names_are_celigo(self):
        from app.services.chat.tool_categories import is_celigo_source

        assert is_celigo_source("celigo_flows") is True
        assert is_celigo_source("celigo.flows") is True
        assert is_celigo_source("celigo_integrations") is True
        assert is_celigo_source("celigo.flow_errors") is True

    def test_external_celigo_read_tool_is_celigo(self):
        from app.services.chat.tool_categories import is_celigo_source

        connector_id = "ab" * 16  # 32 hex chars, matches _make_ext_tool_name's format
        assert is_celigo_source(f"ext__{connector_id}__list_flows") is True

    def test_unrelated_tool_is_not_celigo(self):
        from app.services.chat.tool_categories import is_celigo_source

        assert is_celigo_source("netsuite_suiteql") is False
        assert is_celigo_source("bigquery_sql") is False


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
