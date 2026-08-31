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


class TestCeligoIsNotANetSuiteSource:
    """MAJOR 5 (T2 gate round 4, PR #202): the ``{"data_table", "financial"}``
    branch below predates Celigo, and its comment still claims "bigquery is its
    own category so we only land here for NetSuite".

    That stopped being true when Celigo's read tools were categorised as
    ``data_table`` (so their row output gets intercepted into a table instead of
    being restated by the model). The pin then treated a Celigo call as a
    NetSuite call: a Celigo-only turn pinned the session to NetSuite, and the
    next turn's system prompt told the model "Previous queries in this session
    used NetSuite. For follow-up questions, prefer NetSuite" -- after the user
    had been asking about Celigo flows.

    Celigo is a third source with no pin value of its own (build_source_pin_hint
    only names BigQuery and NetSuite), so it contributes NOTHING to the pin --
    the same treatment metric_compute's source-agnostic `expression` backend
    already gets.
    """

    CELIGO = "ext__c0ffee00c0ffee00c0ffee00c0ffee00__list_flows"

    def test_a_celigo_only_turn_leaves_the_pin_alone(self):
        assert _compute_source_pin_update([{"tool": self.CELIGO}]) == "leave_pin"

    def test_celigo_does_not_make_a_bigquery_turn_look_mixed(self):
        """Previously cleared the pin, as if the user had switched sources."""
        calls = [{"tool": self.CELIGO}, {"tool": "bigquery_sql"}]
        assert _compute_source_pin_update(calls) == "bigquery"

    def test_celigo_alongside_netsuite_still_pins_netsuite(self):
        calls = [{"tool": self.CELIGO}, {"tool": "netsuite_suiteql"}]
        assert _compute_source_pin_update(calls) == "netsuite"

    def test_a_celigo_write_tool_name_is_not_treated_as_a_source_either(self):
        """Unreachable in practice (both guard layers refuse it), but the
        classifier must not fall through to the NetSuite branch on a name it
        does not recognise as a Celigo read."""
        calls = [{"tool": "ext__c0ffee00c0ffee00c0ffee00c0ffee00__delete_resource"}]
        assert _compute_source_pin_update(calls) == "leave_pin"

    def test_a_netsuite_suiteql_mcp_call_is_unaffected(self):
        """The control: the external-MCP branch must keep working for NetSuite."""
        calls = [{"tool": "ext__c0ffee00c0ffee00c0ffee00c0ffee00__ns_runCustomSuiteQL"}]
        assert _compute_source_pin_update(calls) == "netsuite"
