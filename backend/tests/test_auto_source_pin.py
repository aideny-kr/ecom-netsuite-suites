"""source_pin should follow the data source the agent actually used.

If a turn successfully calls bigquery_sql, pin the session to bigquery
so the next turn's routing prefers bi-agent for ambiguous queries.
Mixed turns (both NetSuite and BigQuery tools used) clear the pin so
neither source dominates artificially."""

from app.services.chat.orchestrator import _compute_source_pin_update


class TestComputeSourcePinUpdate:
    def test_bigquery_only_sets_bigquery_pin(self):
        calls = [{"tool_name": "bigquery_sql"}, {"tool_name": "bigquery_schema"}]
        assert _compute_source_pin_update(calls) == "bigquery"

    def test_netsuite_only_sets_netsuite_pin(self):
        calls = [{"tool_name": "netsuite_suiteql"}, {"tool_name": "netsuite_financial_report"}]
        assert _compute_source_pin_update(calls) == "netsuite"

    def test_mixed_calls_returns_none_clear_pin(self):
        calls = [{"tool_name": "netsuite_suiteql"}, {"tool_name": "bigquery_sql"}]
        assert _compute_source_pin_update(calls) is None

    def test_non_data_tools_return_leave_pin(self):
        # rag_search / workspace tools are not data sources — leave the existing pin alone.
        calls = [{"tool_name": "rag_search"}, {"tool_name": "workspace_read_file"}]
        assert _compute_source_pin_update(calls) == "leave_pin"

    def test_empty_log_returns_leave_pin(self):
        assert _compute_source_pin_update([]) == "leave_pin"

    def test_external_mcp_runreport_counts_as_netsuite(self):
        # Oracle MCP ns_runReport is a NetSuite financial query.
        calls = [{"tool_name": "ext__connector1__ns_runReport"}]
        assert _compute_source_pin_update(calls) == "netsuite"
