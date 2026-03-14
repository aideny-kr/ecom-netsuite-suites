"""Tests for _intercept_tool_result() in orchestrator.py."""

import json

from app.services.chat.orchestrator import _intercept_tool_result


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

SAMPLE_SUITEQL_RESULT = {
    "columns": ["tranid", "entity", "amount", "status"],
    "rows": [
        ["SO-1001", "Acme Corp", 5000.00, "Pending"],
        ["SO-1002", "Globex Inc", 3200.50, "Billed"],
        ["SO-1003", "Initech", 1500.00, "Pending"],
    ],
    "row_count": 3,
    "truncated": False,
    "query": "SELECT tranid, entity, amount, status FROM transaction WHERE type = 'SalesOrd'",
    "limit": 1000,
}


def _result_str(data: dict) -> str:
    return json.dumps(data, default=str)


# -- Financial report tests (updated function name + return signature) --


class TestInterceptFinancialReport:
    """Financial report interception — same behavior as before."""

    def test_success(self):
        result_str = _result_str(SAMPLE_FINANCIAL_RESULT)
        event_type, sse_event, condensed = _intercept_tool_result(
            "netsuite.financial_report", result_str
        )
        assert event_type == "financial_report"
        assert sse_event is not None
        assert sse_event["report_type"] == "income_statement"
        assert sse_event["period"] == "Feb 2026"
        assert sse_event["columns"] == ["Account", "Amount"]
        assert sse_event["rows"] == SAMPLE_FINANCIAL_RESULT["items"]
        assert sse_event["summary"] == SAMPLE_FINANCIAL_RESULT["summary"]
        parsed = json.loads(condensed)
        assert parsed["success"] is True
        assert "items" not in parsed
        assert "rows" not in parsed
        assert parsed["total_rows"] == 3

    def test_condensed_has_note(self):
        result_str = _result_str(SAMPLE_FINANCIAL_RESULT)
        _, _, condensed = _intercept_tool_result(
            "netsuite.financial_report", result_str
        )
        parsed = json.loads(condensed)
        assert "note" in parsed
        assert "table" in parsed["note"].lower() or "rebuild" in parsed["note"].lower()

    def test_underscore_tool_name(self):
        result_str = _result_str(SAMPLE_FINANCIAL_RESULT)
        event_type, sse_event, condensed = _intercept_tool_result(
            "netsuite_financial_report", result_str
        )
        assert event_type == "financial_report"
        assert sse_event is not None

    def test_failure_is_noop(self):
        failed = {"success": False, "error": "Query failed"}
        result_str = _result_str(failed)
        event_type, sse_event, returned = _intercept_tool_result(
            "netsuite.financial_report", result_str
        )
        assert event_type is None
        assert sse_event is None
        assert returned == result_str

    def test_invalid_json_is_noop(self):
        event_type, sse_event, returned = _intercept_tool_result(
            "netsuite.financial_report", "Not JSON"
        )
        assert event_type is None
        assert sse_event is None
        assert returned == "Not JSON"


# -- SuiteQL data_table tests (NEW) --


class TestInterceptSuiteQL:
    """SuiteQL query results should emit data_table SSE event."""

    def test_suiteql_success(self):
        result_str = _result_str(SAMPLE_SUITEQL_RESULT)
        event_type, sse_event, condensed = _intercept_tool_result(
            "netsuite_suiteql", result_str
        )
        assert event_type == "data_table"
        assert sse_event is not None
        assert sse_event["columns"] == ["tranid", "entity", "amount", "status"]
        assert sse_event["rows"] == SAMPLE_SUITEQL_RESULT["rows"]
        assert sse_event["row_count"] == 3
        assert sse_event["query"] == SAMPLE_SUITEQL_RESULT["query"]

    def test_suiteql_dot_name(self):
        result_str = _result_str(SAMPLE_SUITEQL_RESULT)
        event_type, sse_event, condensed = _intercept_tool_result(
            "netsuite.suiteql", result_str
        )
        assert event_type == "data_table"
        assert sse_event is not None

    def test_condensed_has_no_rows(self):
        result_str = _result_str(SAMPLE_SUITEQL_RESULT)
        _, _, condensed = _intercept_tool_result(
            "netsuite_suiteql", result_str
        )
        parsed = json.loads(condensed)
        assert "rows" not in parsed
        assert parsed["row_count"] == 3
        assert "note" in parsed

    def test_condensed_preserves_columns(self):
        """LLM should know the columns to provide meaningful commentary."""
        result_str = _result_str(SAMPLE_SUITEQL_RESULT)
        _, _, condensed = _intercept_tool_result(
            "netsuite_suiteql", result_str
        )
        parsed = json.loads(condensed)
        assert parsed["columns"] == ["tranid", "entity", "amount", "status"]

    def test_suiteql_error_is_noop(self):
        error_result = {"error": True, "message": "Invalid column name"}
        result_str = _result_str(error_result)
        event_type, sse_event, returned = _intercept_tool_result(
            "netsuite_suiteql", result_str
        )
        assert event_type is None
        assert sse_event is None
        assert returned == result_str

    def test_suiteql_string_error_is_noop(self):
        error_result = {"error": "Something broke"}
        result_str = _result_str(error_result)
        event_type, sse_event, returned = _intercept_tool_result(
            "netsuite_suiteql", result_str
        )
        assert event_type is None
        assert sse_event is None
        assert returned == result_str

    def test_suiteql_empty_rows(self):
        """Empty results should still emit data_table (shows 'no data' in UI)."""
        empty_result = {
            "columns": ["tranid"],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "query": "SELECT tranid FROM transaction WHERE 1=0",
            "limit": 1000,
        }
        result_str = _result_str(empty_result)
        event_type, sse_event, condensed = _intercept_tool_result(
            "netsuite_suiteql", result_str
        )
        assert event_type == "data_table"
        assert sse_event is not None
        assert sse_event["rows"] == []
        assert sse_event["row_count"] == 0

    def test_suiteql_invalid_json_is_noop(self):
        event_type, sse_event, returned = _intercept_tool_result(
            "netsuite_suiteql", "Not JSON"
        )
        assert event_type is None
        assert sse_event is None
        assert returned == "Not JSON"

    def test_suiteql_missing_columns_is_noop(self):
        """Result without columns array should not be intercepted."""
        result_str = _result_str({"rows": [[1, 2]], "row_count": 1})
        event_type, sse_event, returned = _intercept_tool_result(
            "netsuite_suiteql", result_str
        )
        assert event_type is None
        assert sse_event is None
        assert returned == result_str

    def test_ext_mcp_suiteql_tool(self):
        """External MCP SuiteQL tools (ext__<hex>__...) should be intercepted."""
        result_str = _result_str(SAMPLE_SUITEQL_RESULT)
        event_type, sse_event, condensed = _intercept_tool_result(
            "ext__abc123def__ns_runcustomsuiteql", result_str
        )
        assert event_type == "data_table"
        assert sse_event is not None
        assert sse_event["columns"] == SAMPLE_SUITEQL_RESULT["columns"]

    def test_ext_mcp_items_format(self):
        """External MCP returns {items: [{col: val}, ...]} — should convert to columns/rows."""
        mcp_result = {
            "items": [
                {"tranid": "SO-1001", "entity": "Acme Corp", "amount": 5000.00},
                {"tranid": "SO-1002", "entity": "Globex Inc", "amount": 3200.50},
            ]
        }
        result_str = _result_str(mcp_result)
        event_type, sse_event, condensed = _intercept_tool_result(
            "ext__abc123def__ns_runcustomsuiteql", result_str
        )
        assert event_type == "data_table"
        assert sse_event is not None
        assert sse_event["columns"] == ["tranid", "entity", "amount"]
        assert sse_event["rows"] == [
            ["SO-1001", "Acme Corp", 5000.00],
            ["SO-1002", "Globex Inc", 3200.50],
        ]
        assert sse_event["row_count"] == 2

    def test_ext_mcp_empty_items_is_noop(self):
        """External MCP with empty items list should not be intercepted."""
        result_str = _result_str({"items": []})
        event_type, sse_event, returned = _intercept_tool_result(
            "ext__abc123def__ns_runcustomsuiteql", result_str
        )
        assert event_type is None
        assert sse_event is None
        assert returned == result_str


class TestInterceptNonMatchingTool:
    """Non-data tools should be untouched."""

    def test_rag_search_is_noop(self):
        result_str = _result_str({"chunks": [{"text": "hello"}]})
        event_type, sse_event, returned = _intercept_tool_result(
            "rag_search", result_str
        )
        assert event_type is None
        assert sse_event is None
        assert returned == result_str

    def test_workspace_tool_is_noop(self):
        result_str = _result_str({"files": ["a.js", "b.js"]})
        event_type, sse_event, returned = _intercept_tool_result(
            "workspace.list_files", result_str
        )
        assert event_type is None
        assert sse_event is None
        assert returned == result_str
