"""Tests for programmatic 'stop when you have data' enforcement (Fix 5 — 10x Agent Quality).

After a tool returns successful data, the agent should get a system nudge
to present results instead of running more queries.
"""

import json

import pytest

from app.services.chat.agents.base_agent import _has_successful_data_result


class TestHasSuccessfulDataResult:
    """Unit tests for _has_successful_data_result()."""

    def test_local_suiteql_with_rows(self):
        """Local SuiteQL format with rows should return True."""
        result_str = json.dumps({
            "columns": ["tranid", "status"],
            "rows": [["RMA123", "E"]],
            "row_count": 1,
        })
        assert _has_successful_data_result([result_str]) is True

    def test_local_suiteql_empty_rows(self):
        """Local SuiteQL with 0 rows should return False."""
        result_str = json.dumps({
            "columns": ["tranid", "status"],
            "rows": [],
            "row_count": 0,
        })
        assert _has_successful_data_result([result_str]) is False

    def test_external_mcp_with_data(self):
        """External MCP format with data should return True."""
        result_str = json.dumps({
            "data": [{"tranid": "RMA123", "status": "E"}],
            "queryExecuted": "SELECT ...",
            "resultCount": 1,
        })
        assert _has_successful_data_result([result_str]) is True

    def test_external_mcp_empty_data(self):
        """External MCP with empty data should return False."""
        result_str = json.dumps({
            "data": [],
            "queryExecuted": "SELECT ...",
            "resultCount": 0,
        })
        assert _has_successful_data_result([result_str]) is False

    def test_financial_report_with_items(self):
        """Financial report format with items should return True."""
        result_str = json.dumps({
            "items": [{"account": "Revenue", "amount": 1000}],
            "summary": {"total_revenue": 1000},
        })
        assert _has_successful_data_result([result_str]) is True

    def test_error_result(self):
        """Tool error should return False."""
        result_str = json.dumps({
            "error": "Unknown identifier 'badcol'",
        })
        assert _has_successful_data_result([result_str]) is False

    def test_error_true_with_rows(self):
        """Error=True even with rows should return False."""
        result_str = json.dumps({
            "error": True,
            "message": "Query failed",
            "rows": [["x"]],
        })
        assert _has_successful_data_result([result_str]) is False

    def test_non_json_result(self):
        """Non-JSON result should return False (not crash)."""
        assert _has_successful_data_result(["not json"]) is False

    def test_empty_list(self):
        """Empty list of results should return False."""
        assert _has_successful_data_result([]) is False

    def test_mixed_results(self):
        """If any result has data, return True even if others failed."""
        error_str = json.dumps({"error": "failed"})
        success_str = json.dumps({
            "columns": ["id"], "rows": [["1"]], "row_count": 1,
        })
        assert _has_successful_data_result([error_str, success_str]) is True

    def test_metadata_result_not_data(self):
        """Metadata results (no rows/data/items) should return False."""
        result_str = json.dumps({
            "record_type": "transaction",
            "fields": [{"name": "tranid", "type": "varchar"}],
        })
        assert _has_successful_data_result([result_str]) is False


class TestStopNudgeMessage:
    """The nudge text should be clear and not block legitimate error recovery."""

    def test_nudge_constant_exists(self):
        from app.services.chat.agents.base_agent import _DATA_SUCCESS_NUDGE
        assert "SYSTEM" in _DATA_SUCCESS_NUDGE
        assert "present" in _DATA_SUCCESS_NUDGE.lower() or "results" in _DATA_SUCCESS_NUDGE.lower()
