"""Unit tests for `_compute_need_patterns` — the pattern-retrieval gate.

The gate decides whether to retrieve seeded SuiteQL/BigQuery patterns
during context assembly. Pre-2026-04-16 it was tied to ContextNeed.DATA
only; FULL (investigation) queries deliberately skipped patterns. With
admin-seeded high-quality patterns, that's wrong — patterns should
retrieve whenever a SQL query tool is in the available tool set,
regardless of the context-need classifier's choice.

Incident: 2026-04-16 staging — Framework's 6 shipping-country patterns
were never retrieved because the user's question 'what are the 4 new
countries we recently launched?' classified as FULL, and the gate
skipped patterns under FULL by design.
"""

import pytest

from app.services.chat.orchestrator import ContextNeed, _compute_need_patterns


class TestComputeNeedPatterns:
    def test_data_with_suiteql_returns_true(self):
        """Existing DATA behavior is preserved when SuiteQL is connected."""
        assert _compute_need_patterns(ContextNeed.DATA, {"netsuite_suiteql"}) is True

    def test_data_with_bigquery_returns_true(self):
        assert _compute_need_patterns(ContextNeed.DATA, {"bigquery_sql"}) is True

    def test_full_with_suiteql_returns_true(self):
        """The fix: FULL no longer skips patterns when SuiteQL is connected."""
        assert _compute_need_patterns(ContextNeed.FULL, {"netsuite_suiteql"}) is True

    def test_full_with_bigquery_returns_true(self):
        assert _compute_need_patterns(ContextNeed.FULL, {"bigquery_sql"}) is True

    def test_full_with_both_sql_tools_returns_true(self):
        assert _compute_need_patterns(ContextNeed.FULL, {"netsuite_suiteql", "bigquery_sql"}) is True

    def test_full_with_no_sql_tools_returns_false(self):
        """No SQL tool in the toolset → no patterns to retrieve."""
        assert _compute_need_patterns(ContextNeed.FULL, {"web_search", "pricing_convert"}) is False

    def test_data_with_no_sql_tools_returns_false(self):
        """Even DATA shouldn't request patterns when there are no SQL tools."""
        assert _compute_need_patterns(ContextNeed.DATA, {"pricing_convert"}) is False

    def test_ext_mcp_suiteql_tool_returns_true(self):
        """External MCP SuiteQL tool names like ext__abc-123__ns_runCustomSuiteQL match."""
        assert _compute_need_patterns(ContextNeed.FULL, {"ext__abc-123__ns_runCustomSuiteQL"}) is True

    def test_ext_mcp_non_suiteql_tool_returns_false(self):
        """ext__ prefix alone doesn't qualify — must be a SuiteQL variant."""
        assert (
            _compute_need_patterns(ContextNeed.FULL, {"ext__abc-123__ns_runReport"})
            is False
        )

    def test_docs_with_suiteql_returns_true(self):
        """Tool presence wins over context-need across the board."""
        assert _compute_need_patterns(ContextNeed.DOCS, {"netsuite_suiteql"}) is True

    def test_workspace_with_suiteql_returns_true(self):
        assert _compute_need_patterns(ContextNeed.WORKSPACE, {"netsuite_suiteql"}) is True

    def test_financial_with_suiteql_returns_true(self):
        assert _compute_need_patterns(ContextNeed.FINANCIAL, {"netsuite_suiteql"}) is True

    def test_empty_tool_names_returns_false(self):
        assert _compute_need_patterns(ContextNeed.FULL, set()) is False
