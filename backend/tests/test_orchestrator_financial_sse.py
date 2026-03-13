"""Tests for _intercept_financial_report() in orchestrator.py."""

import json
import pytest

from app.services.chat.orchestrator import _intercept_financial_report


# -- Fixtures --

SAMPLE_FINANCIAL_RESULT = {
    "success": True,
    "report_type": "income_statement",
    "period": "Feb 2026",
    "columns": ["Account", "Amount"],
    "items": [
        {"account": "Revenue", "amount": 100000},
        {"account": "COGS", "amount": -40000},
        {"account": "Net Income", "amount": 60000},
    ],
    "summary": {"total_revenue": 100000, "net_income": 60000},
}


def _result_str(data: dict) -> str:
    return json.dumps(data, default=str)


class TestInterceptFinancialReportSuccess:
    """Given a successful financial report result, verify interception."""

    def test_intercept_financial_report_success(self):
        result_str = _result_str(SAMPLE_FINANCIAL_RESULT)
        sse_event, condensed = _intercept_financial_report(
            "netsuite.financial_report", result_str
        )

        assert sse_event is not None
        assert sse_event["report_type"] == "income_statement"
        assert sse_event["period"] == "Feb 2026"
        assert sse_event["columns"] == ["Account", "Amount"]
        assert sse_event["rows"] == SAMPLE_FINANCIAL_RESULT["items"]
        assert sse_event["summary"] == SAMPLE_FINANCIAL_RESULT["summary"]

        # condensed should be valid JSON
        parsed = json.loads(condensed)
        assert parsed["success"] is True
        assert parsed["report_type"] == "income_statement"
        assert parsed["period"] == "Feb 2026"
        assert parsed["total_rows"] == 3

    def test_sse_event_has_full_rows(self):
        """The SSE event must contain the full rows for frontend rendering."""
        result_str = _result_str(SAMPLE_FINANCIAL_RESULT)
        sse_event, _ = _intercept_financial_report(
            "netsuite.financial_report", result_str
        )

        assert sse_event is not None
        assert len(sse_event["rows"]) == 3
        assert sse_event["rows"][0] == {"account": "Revenue", "amount": 100000}

    def test_condensed_result_has_no_rows(self):
        """The condensed result must NOT contain full rows/items."""
        result_str = _result_str(SAMPLE_FINANCIAL_RESULT)
        _, condensed = _intercept_financial_report(
            "netsuite.financial_report", result_str
        )

        parsed = json.loads(condensed)
        assert "items" not in parsed
        assert "rows" not in parsed

    def test_condensed_result_has_note(self):
        """The condensed result must tell the LLM not to rebuild the table."""
        result_str = _result_str(SAMPLE_FINANCIAL_RESULT)
        _, condensed = _intercept_financial_report(
            "netsuite.financial_report", result_str
        )

        parsed = json.loads(condensed)
        assert "note" in parsed
        assert "table" in parsed["note"].lower() or "rebuild" in parsed["note"].lower()


    def test_intercept_with_underscore_tool_name(self):
        """The tool name sent by the LLM uses underscores, not dots."""
        result_str = _result_str(SAMPLE_FINANCIAL_RESULT)
        sse_event, condensed = _intercept_financial_report(
            "netsuite_financial_report", result_str
        )

        assert sse_event is not None
        assert sse_event["report_type"] == "income_statement"
        parsed = json.loads(condensed)
        assert parsed["success"] is True


class TestInterceptFinancialReportNoOp:
    """Cases where interception should be a no-op."""

    def test_intercept_financial_report_not_financial_tool(self):
        result_str = _result_str(SAMPLE_FINANCIAL_RESULT)
        sse_event, returned = _intercept_financial_report(
            "netsuite_suiteql", result_str
        )

        assert sse_event is None
        assert returned == result_str

    def test_intercept_financial_report_failure(self):
        failed = {"success": False, "error": "Query failed"}
        result_str = _result_str(failed)
        sse_event, returned = _intercept_financial_report(
            "netsuite.financial_report", result_str
        )

        assert sse_event is None
        assert returned == result_str

    def test_intercept_financial_report_invalid_json(self):
        result_str = "This is not JSON at all"
        sse_event, returned = _intercept_financial_report(
            "netsuite.financial_report", result_str
        )

        assert sse_event is None
        assert returned == result_str
