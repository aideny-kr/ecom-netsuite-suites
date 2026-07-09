"""Tests for _intercept_tool_result() in orchestrator.py."""

import json
from unittest.mock import patch

import pytest

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
        event_type, sse_event, condensed = _intercept_tool_result("netsuite.financial_report", result_str)
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
        _, _, condensed = _intercept_tool_result("netsuite.financial_report", result_str)
        parsed = json.loads(condensed)
        assert "note" in parsed
        assert "table" in parsed["note"].lower() or "rebuild" in parsed["note"].lower()

    def test_underscore_tool_name(self):
        result_str = _result_str(SAMPLE_FINANCIAL_RESULT)
        event_type, sse_event, condensed = _intercept_tool_result("netsuite_financial_report", result_str)
        assert event_type == "financial_report"
        assert sse_event is not None

    def test_failure_is_noop(self):
        failed = {"success": False, "error": "Query failed"}
        result_str = _result_str(failed)
        event_type, sse_event, returned = _intercept_tool_result("netsuite.financial_report", result_str)
        assert event_type is None
        assert sse_event is None
        assert returned == result_str

    def test_invalid_json_is_noop(self):
        event_type, sse_event, returned = _intercept_tool_result("netsuite.financial_report", "Not JSON")
        assert event_type is None
        assert sse_event is None
        assert returned == "Not JSON"


# -- SuiteQL data_table tests (NEW) --


class TestInterceptSuiteQL:
    """SuiteQL query results should emit data_table SSE event."""

    def test_suiteql_success(self):
        result_str = _result_str(SAMPLE_SUITEQL_RESULT)
        event_type, sse_event, condensed = _intercept_tool_result("netsuite_suiteql", result_str)
        assert event_type == "data_table"
        assert sse_event is not None
        assert sse_event["columns"] == ["tranid", "entity", "amount", "status"]
        assert sse_event["rows"] == SAMPLE_SUITEQL_RESULT["rows"]
        assert sse_event["row_count"] == 3
        assert sse_event["query"] == SAMPLE_SUITEQL_RESULT["query"]

    def test_suiteql_dot_name(self):
        result_str = _result_str(SAMPLE_SUITEQL_RESULT)
        event_type, sse_event, condensed = _intercept_tool_result("netsuite.suiteql", result_str)
        assert event_type == "data_table"
        assert sse_event is not None

    def test_condensed_has_no_rows(self):
        result_str = _result_str(SAMPLE_SUITEQL_RESULT)
        _, _, condensed = _intercept_tool_result("netsuite_suiteql", result_str)
        parsed = json.loads(condensed)
        assert "rows" not in parsed
        assert parsed["row_count"] == 3
        assert "note" in parsed

    def test_condensed_preserves_columns(self):
        """LLM should know the columns to provide meaningful commentary."""
        result_str = _result_str(SAMPLE_SUITEQL_RESULT)
        _, _, condensed = _intercept_tool_result("netsuite_suiteql", result_str)
        parsed = json.loads(condensed)
        assert parsed["columns"] == ["tranid", "entity", "amount", "status"]

    def test_suiteql_error_is_noop(self):
        error_result = {"error": True, "message": "Invalid column name"}
        result_str = _result_str(error_result)
        event_type, sse_event, returned = _intercept_tool_result("netsuite_suiteql", result_str)
        assert event_type is None
        assert sse_event is None
        assert returned == result_str

    def test_suiteql_string_error_is_noop(self):
        error_result = {"error": "Something broke"}
        result_str = _result_str(error_result)
        event_type, sse_event, returned = _intercept_tool_result("netsuite_suiteql", result_str)
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
        event_type, sse_event, condensed = _intercept_tool_result("netsuite_suiteql", result_str)
        assert event_type == "data_table"
        assert sse_event is not None
        assert sse_event["rows"] == []
        assert sse_event["row_count"] == 0

    def test_suiteql_invalid_json_is_noop(self):
        event_type, sse_event, returned = _intercept_tool_result("netsuite_suiteql", "Not JSON")
        assert event_type is None
        assert sse_event is None
        assert returned == "Not JSON"

    def test_suiteql_missing_columns_is_noop(self):
        """Result without columns array should not be intercepted."""
        result_str = _result_str({"rows": [[1, 2]], "row_count": 1})
        event_type, sse_event, returned = _intercept_tool_result("netsuite_suiteql", result_str)
        assert event_type is None
        assert sse_event is None
        assert returned == result_str

    def test_ext_mcp_suiteql_tool(self):
        """External MCP SuiteQL tools (ext__<hex>__...) should be intercepted."""
        result_str = _result_str(SAMPLE_SUITEQL_RESULT)
        event_type, sse_event, condensed = _intercept_tool_result("ext__abc123def__ns_runcustomsuiteql", result_str)
        assert event_type == "data_table"
        assert sse_event is not None
        assert sse_event["columns"] == SAMPLE_SUITEQL_RESULT["columns"]

    def test_ext_mcp_data_format(self):
        """External MCP returns {data: [{col: val}, ...], queryExecuted: ...} — real format."""
        mcp_result = {
            "method": "custom_suiteql",
            "description": "Get top sales orders",
            "queryExecuted": "SELECT t.tranid, t.total FROM transaction t",
            "resultCount": 2,
            "data": [
                {"tranid": "SO-1001", "total": 5000.00},
                {"tranid": "SO-1002", "total": 3200.50},
            ],
            "pageSize": 1000,
            "numberOfPages": 1,
        }
        result_str = _result_str(mcp_result)
        event_type, sse_event, condensed = _intercept_tool_result("ext__abc123def__ns_runcustomsuiteql", result_str)
        assert event_type == "data_table"
        assert sse_event is not None
        assert sse_event["columns"] == ["tranid", "total"]
        assert sse_event["rows"] == [
            ["SO-1001", 5000.00],
            ["SO-1002", 3200.50],
        ]
        assert sse_event["row_count"] == 2
        assert sse_event["query"] == "SELECT t.tranid, t.total FROM transaction t"

    def test_ext_mcp_items_format(self):
        """External MCP returns {items: [{col: val}, ...]} — fallback format."""
        mcp_result = {
            "items": [
                {"tranid": "SO-1001", "entity": "Acme Corp", "amount": 5000.00},
                {"tranid": "SO-1002", "entity": "Globex Inc", "amount": 3200.50},
            ]
        }
        result_str = _result_str(mcp_result)
        event_type, sse_event, condensed = _intercept_tool_result("ext__abc123def__ns_runcustomsuiteql", result_str)
        assert event_type == "data_table"
        assert sse_event is not None
        assert sse_event["columns"] == ["tranid", "entity", "amount"]
        assert sse_event["rows"] == [
            ["SO-1001", "Acme Corp", 5000.00],
            ["SO-1002", "Globex Inc", 3200.50],
        ]
        assert sse_event["row_count"] == 2

    def test_ext_mcp_empty_data_is_noop(self):
        """External MCP with empty data list should not be intercepted."""
        result_str = _result_str({"data": []})
        event_type, sse_event, returned = _intercept_tool_result("ext__abc123def__ns_runcustomsuiteql", result_str)
        assert event_type is None
        assert sse_event is None
        assert returned == result_str

    def test_ext_mcp_empty_items_is_noop(self):
        """External MCP with empty items list should not be intercepted."""
        result_str = _result_str({"items": []})
        event_type, sse_event, returned = _intercept_tool_result("ext__abc123def__ns_runcustomsuiteql", result_str)
        assert event_type is None
        assert sse_event is None
        assert returned == result_str


class TestInterceptNonMatchingTool:
    """Non-data tools should be untouched."""

    def test_rag_search_is_noop(self):
        result_str = _result_str({"chunks": [{"text": "hello"}]})
        event_type, sse_event, returned = _intercept_tool_result("rag_search", result_str)
        assert event_type is None
        assert sse_event is None
        assert returned == result_str

    def test_workspace_tool_is_noop(self):
        result_str = _result_str({"files": ["a.js", "b.js"]})
        event_type, sse_event, returned = _intercept_tool_result("workspace.list_files", result_str)
        assert event_type is None
        assert sse_event is None
        assert returned == result_str


class TestResultIdContract:
    """Gate cluster A: the LLM must be SHOWN the result_id (r1, r2...) that the
    same-turn report.compose resolver expects. _intercept_tool_result stamps a
    caller-supplied result_id into BOTH the condensed LLM string AND the SSE
    event data for the data_table / financial_report / metric paths."""

    def test_data_table_condensed_carries_result_id(self):
        result_str = _result_str(SAMPLE_SUITEQL_RESULT)
        event_type, sse_event, condensed = _intercept_tool_result("netsuite_suiteql", result_str, result_id="r1")
        assert event_type == "data_table"
        # The model reads the condensed JSON string — it must contain the handle.
        parsed = json.loads(condensed)
        assert parsed["result_id"] == "r1"
        # The SSE event the frontend gets carries it too.
        assert sse_event["result_id"] == "r1"

    def test_data_table_full_context_carries_result_id(self):
        from app.services.chat.orchestrator import ContextNeed

        result_str = _result_str(SAMPLE_SUITEQL_RESULT)
        _, sse_event, condensed = _intercept_tool_result(
            "netsuite_suiteql", result_str, context_need=ContextNeed.FULL, result_id="r2"
        )
        assert json.loads(condensed)["result_id"] == "r2"
        assert sse_event["result_id"] == "r2"

    def test_financial_report_condensed_carries_result_id(self):
        result_str = _result_str(SAMPLE_FINANCIAL_RESULT)
        event_type, sse_event, condensed = _intercept_tool_result(
            "netsuite.financial_report", result_str, result_id="r1"
        )
        assert event_type == "financial_report"
        assert json.loads(condensed)["result_id"] == "r1"
        assert sse_event["result_id"] == "r1"

    def test_metric_condensed_carries_result_id(self):
        metric = {
            "columns": ["Metric", "Value", "Unit", "Period"],
            "rows": [["Revenue", 142800, "USD", "Q2 2026"]],
            "row_count": 1,
            "suppress_llm_value": True,
            "source_kind": "suiteql",
        }
        event_type, sse_event, condensed = _intercept_tool_result("metric_compute", _result_str(metric), result_id="r3")
        assert event_type == "data_table"
        parsed = json.loads(condensed)
        assert parsed["result_id"] == "r3"
        # The metric trust boundary still holds — the number must NOT leak.
        assert "142800" not in condensed
        assert sse_event["result_id"] == "r3"

    def test_no_result_id_keeps_string_clean(self):
        """When no result_id is supplied (e.g. the e2e calls the 2-arg form),
        the condensed string carries no result_id key — backward compatible."""
        result_str = _result_str(SAMPLE_SUITEQL_RESULT)
        _, sse_event, condensed = _intercept_tool_result("netsuite_suiteql", result_str)
        assert "result_id" not in json.loads(condensed)
        assert "result_id" not in sse_event


# -- Re-gate r2: the SINGLE id-assignment invariant (_make_tool_interceptor) --


class TestSingleIdAssignmentInvariant:
    """Re-gate r2 (findings #3/#6/#11/#15): ``_make_tool_interceptor`` must assign
    a turn-scoped result_id IFF ``extract_result_payload`` returns non-None — the
    SAME criterion that decides whether the sidecar (and the persisted fallback)
    can resolve that id. The previous criterion (``peek_type in _DATA_RESULT_EVENTS``)
    could disagree with the payload extractor, leaving an id that resolves to
    nothing (or shifting the dense r1,r2… numbering)."""

    def test_data_key_result_gets_id_and_payload(self):
        """An external-MCP ``{"data": [...]}`` result both gets stamped a result_id
        AND yields a non-None payload to the sidecar callback — they must agree."""
        from app.services.chat.orchestrator import _make_tool_interceptor

        captured: dict = {}

        def _cb(tool_name, event_type_str, event_data, result_id=None, params=None, result_str=None, full_payload=None):
            captured["result_id"] = result_id
            captured["payload"] = full_payload

        interceptor = _make_tool_interceptor(cache_callback=_cb)
        mcp_result = {
            "data": [
                {"tranid": "SO-1001", "total": 5000.00},
                {"tranid": "SO-1002", "total": 3200.50},
            ]
        }
        sse_tuple, llm_str = interceptor("ext__abc__ns_runcustomsuiteql", json.dumps(mcp_result))
        assert sse_tuple is not None
        event_type, sse_event = sse_tuple
        assert event_type == "data_table"
        # The LLM is shown r1 ...
        assert json.loads(llm_str)["result_id"] == "r1"
        assert sse_event["result_id"] == "r1"
        # ... AND the sidecar callback got a non-None payload keyed by the same id.
        assert captured["result_id"] == "r1"
        assert captured["payload"] is not None
        assert captured["payload"]["columns"] == ["tranid", "total"]

    def test_non_data_tool_consumes_no_counter_slot(self):
        """A non-data tool (no extractable payload) must NOT advance the counter —
        the ids stay dense (r1, r2, ...) so the model's positional reference is
        unambiguous."""
        from app.services.chat.orchestrator import _make_tool_interceptor

        seen_ids: list = []

        def _cb(tool_name, event_type_str, event_data, result_id=None, params=None, result_str=None, full_payload=None):
            seen_ids.append((tool_name, result_id))

        interceptor = _make_tool_interceptor(cache_callback=_cb)
        # A rag_search result has no extractable payload — it must not be intercepted
        # NOR consume a counter slot.
        interceptor("rag_search", json.dumps({"chunks": [{"text": "hi"}]}))
        # Then a real data_table → must be r1 (not r2).
        sse_tuple, llm_str = interceptor("netsuite_suiteql", _result_str(SAMPLE_SUITEQL_RESULT))
        assert sse_tuple is not None
        assert json.loads(llm_str)["result_id"] == "r1"
        # The callback only fired for the data tool, with r1.
        assert seen_ids == [("netsuite_suiteql", "r1")]


class TestConversationOrdinalIds:
    """re-gate r2 (findings #5/#9/#13): result ids are CONVERSATION-ORDINAL, not
    per-turn. ``_make_tool_interceptor`` accepts a ``start_count`` (K) = the number
    of payload-bearing results ALREADY produced in this conversation's history;
    this turn's first data result is r(K+1), so turn B's first result never reuses
    turn A's r1. One id space across the whole conversation, shared with the
    persisted-fallback resolver (which numbers the same population 1..K)."""

    def test_counter_starts_after_prior_conversation_results(self):
        from app.services.chat.orchestrator import _make_tool_interceptor

        seen_ids: list = []

        def _cb(tool_name, event_type_str, event_data, result_id=None, params=None, result_str=None, full_payload=None):
            seen_ids.append(result_id)

        # 3 payload-bearing results already produced earlier in this conversation.
        interceptor = _make_tool_interceptor(cache_callback=_cb, start_count=3)
        _, llm_str = interceptor("netsuite_suiteql", _result_str(SAMPLE_SUITEQL_RESULT))
        # This turn's first data result is r4, NOT r1.
        assert json.loads(llm_str)["result_id"] == "r4"
        # A second result this turn → r5.
        _, llm_str2 = interceptor("netsuite_suiteql", _result_str(SAMPLE_SUITEQL_RESULT))
        assert json.loads(llm_str2)["result_id"] == "r5"
        assert seen_ids == ["r4", "r5"]

    def test_default_start_count_is_zero_backward_compatible(self):
        """With no prior history (start_count omitted / 0), the first result is r1 —
        unchanged from the per-turn behavior for a first-turn conversation."""
        from app.services.chat.orchestrator import _make_tool_interceptor

        interceptor = _make_tool_interceptor()
        _, llm_str = interceptor("netsuite_suiteql", _result_str(SAMPLE_SUITEQL_RESULT))
        assert json.loads(llm_str)["result_id"] == "r1"

    def test_two_turns_do_not_collide_on_r1(self):
        """The cross-turn collision the fix targets: turn A produces r1; turn B,
        seeded with start_count=1 (turn A's one result is now in history), produces
        r2 — turn B's first result does NOT overwrite turn A's r1."""
        from app.services.chat.orchestrator import _make_tool_interceptor

        turn_a = _make_tool_interceptor()  # start_count=0
        _, a_str = turn_a("netsuite_suiteql", _result_str(SAMPLE_SUITEQL_RESULT))
        assert json.loads(a_str)["result_id"] == "r1"

        # Turn B: turn A's single payload-bearing result is now persisted history → K=1.
        turn_b = _make_tool_interceptor(start_count=1)
        _, b_str = turn_b("netsuite_suiteql", _result_str(SAMPLE_SUITEQL_RESULT))
        assert json.loads(b_str)["result_id"] == "r2", (
            "turn B's first result must be r2 (conversation-ordinal), never reusing turn A's r1"
        )


class TestPreTruncationSidecarPayload:
    """Re-gate r2 (finding #10): the LLM-facing string may be row-capped, but the
    sidecar/persisted payload the report composer resolves must be the FULL,
    UNCAPPED result. The interceptor must extract the payload from the ORIGINAL
    (pre-truncation) result string passed as ``full_result_str``, not the 500-row
    LLM-facing one."""

    def test_sidecar_uses_full_string_while_llm_stays_capped(self):
        """The interceptor receives BOTH the (truncated) LLM string AND the original
        full string. The sidecar payload carries ALL 600 rows; the LLM-facing
        condensed string only reflects the capped one."""
        from app.services.chat.orchestrator import _make_tool_interceptor

        captured: dict = {}

        def _cb(tool_name, event_type_str, event_data, result_id=None, params=None, result_str=None, full_payload=None):
            captured["payload"] = full_payload
            captured["result_id"] = result_id

        interceptor = _make_tool_interceptor(cache_callback=_cb)

        # 600 rows in the FULL string; the LLM string is a 30-row "capped" copy
        # (mirroring _truncate_tool_result + the 30-row preview the LLM condense uses).
        full_rows = [[f"SO-{i:04d}", i * 1.5] for i in range(600)]
        full_result = {
            "columns": ["tranid", "amount"],
            "rows": full_rows,
            "row_count": 600,
            "query": "SELECT tranid, amount FROM transaction",
        }
        capped_rows = full_rows[:30]
        capped_result = {**full_result, "rows": capped_rows, "rows_truncated": True}

        sse_tuple, _llm_str = interceptor(
            "netsuite_suiteql",
            json.dumps(capped_result, default=str),  # LLM-facing (truncated)
            None,
            json.dumps(full_result, default=str),  # ORIGINAL full string
        )
        assert sse_tuple is not None
        # The sidecar payload must carry ALL 600 rows (extracted from the full string).
        assert captured["result_id"] == "r1"
        assert captured["payload"] is not None
        assert captured["payload"]["row_count"] == 600
        assert len(captured["payload"]["rows"]) == 600

    def test_falls_back_to_result_str_when_no_full_provided(self):
        """When no full_result_str is supplied (older/test callers), the payload is
        extracted from result_str — backward compatible."""
        from app.services.chat.orchestrator import _make_tool_interceptor

        captured: dict = {}

        def _cb(tool_name, event_type_str, event_data, result_id=None, params=None, result_str=None, full_payload=None):
            captured["payload"] = full_payload

        interceptor = _make_tool_interceptor(cache_callback=_cb)
        interceptor("netsuite_suiteql", _result_str(SAMPLE_SUITEQL_RESULT))
        assert captured["payload"] is not None
        assert captured["payload"]["row_count"] == 3

    def test_base_agent_passes_original_string_to_interceptor(self):
        """The base_agent helper forwards (tool, llm_str, params, full_str) and tolerates
        narrower-arity interceptors (the seam that feeds finding #10's fix)."""
        from app.services.chat.agents.base_agent import _call_tool_result_interceptor

        seen: dict = {}

        # 4-arg interceptor (the production _make_tool_interceptor shape).
        def four_arg(tool_name, result_str, params=None, full_result_str=None):
            seen["full"] = full_result_str
            seen["llm"] = result_str
            return None, result_str

        _call_tool_result_interceptor(four_arg, "netsuite_suiteql", '{"capped":1}', {"q": 1}, '{"full":600}')
        assert seen == {"full": '{"full":600}', "llm": '{"capped":1}'}

        # 2-arg legacy interceptor still works (TypeError fallback).
        calls: list = []

        def two_arg(tool_name, result_str):
            calls.append((tool_name, result_str))
            return None, result_str

        out = _call_tool_result_interceptor(two_arg, "x", "capped", {}, "full")
        assert calls == [("x", "capped")]
        assert out == (None, "capped")


# -- Re-gate r3 (final): UNIFIED slot criterion = extractable AND stamped --


SAMPLE_LIST_ALL_REPORTS = [
    {"id": "1", "name": "Income Statement", "type": "FINANCIAL"},
    {"id": "2", "name": "Balance Sheet", "type": "FINANCIAL"},
]


class TestUnifiedSlotCriterion:
    """Re-gate r3 (findings #1/#2/#5): a result earns an r-id slot IFF
    ``extract_result_payload`` returns non-None AND the intercept STAMPS the id
    into the LLM-facing string (event_type is a stamped data event — data_table
    or financial_report — so the model actually SEES the id). A payload-bearing
    but hidden/'other'-category tool (``ns_listAllReports`` — a top-level list the
    extractor happily reads but the intercept classifies as 'other' and never
    stamps/SSE's) gets NO counter slot, NO sidecar, NO persisted result_payload —
    keeping the visible id sequence dense and aligned with the persisted fallback
    (which counts result_payload-bearing calls)."""

    def test_list_all_reports_gets_no_slot_no_sidecar(self):
        """ns_listAllReports-shaped result (top-level list, category 'other'):
        extract_result_payload returns non-None, but the intercept emits NO
        stamped data event, so it must NOT advance the counter NOR write a sidecar
        entry. A FOLLOWING suiteql result gets the NEXT dense id r1 (not r2)."""
        from app.services.chat.orchestrator import _make_tool_interceptor

        seen: list = []

        def _cb(tool_name, event_type_str, event_data, result_id=None, params=None, result_str=None, full_payload=None):
            seen.append((tool_name, event_type_str, result_id))

        interceptor = _make_tool_interceptor(cache_callback=_cb)
        # 1) ns_listAllReports: top-level list, category 'other' → no stamp.
        sse_tuple, _ = interceptor("ext__abc__ns_listallreports", json.dumps(SAMPLE_LIST_ALL_REPORTS))
        assert sse_tuple is None, "ns_listAllReports must not produce an SSE data event"
        # No counter slot, no sidecar callback for the hidden tool.
        assert seen == [], "a payload-bearing but unstamped 'other' tool must get NO sidecar/slot"

        # 2) A real suiteql result must be the FIRST dense id r1, not r2.
        sse2, llm_str = interceptor("netsuite_suiteql", _result_str(SAMPLE_SUITEQL_RESULT))
        assert sse2 is not None
        assert json.loads(llm_str)["result_id"] == "r1", (
            "the next stamped result must take the next dense id (r1) — the hidden "
            "ns_listAllReports must not have consumed r1"
        )
        assert seen == [("netsuite_suiteql", "data_table", "r1")]

    def test_financial_report_stamped_id_is_resolvable(self):
        """Finding #5 (financial direction): a financial_report result IS stamped,
        so the SAME criterion must grant it a slot — extract_result_payload must
        return a non-None payload for the financial shape so the stamped id never
        dangles. Resolves via the sidecar (full_payload) here."""
        from app.services.chat.orchestrator import _make_tool_interceptor

        captured: dict = {}

        def _cb(tool_name, event_type_str, event_data, result_id=None, params=None, result_str=None, full_payload=None):
            captured["event_type"] = event_type_str
            captured["result_id"] = result_id
            captured["payload"] = full_payload

        interceptor = _make_tool_interceptor(cache_callback=_cb)
        sse_tuple, llm_str = interceptor("netsuite_financial_report", _result_str(SAMPLE_FINANCIAL_RESULT))
        assert sse_tuple is not None
        event_type, _ = sse_tuple
        assert event_type == "financial_report"
        # The stamped id the LLM sees ...
        assert json.loads(llm_str)["result_id"] == "r1"
        # ... must resolve to a non-None sidecar payload (no dangling id).
        assert captured["event_type"] == "financial_report"
        assert captured["result_id"] == "r1"
        assert captured["payload"] is not None, "financial stamped id must carry a resolvable payload"
        assert captured["payload"]["kind"] == "table"

    def test_empty_financial_report_stamped_id_resolves(self):
        """A successful-but-EMPTY financial report (zero-activity period) still
        fires the financial_report SSE event (stamped), so it MUST get a slot AND
        a resolvable (empty-rows) payload — pre-fix the empty shape produced None
        from extract_result_payload, dangling the stamped id."""
        from app.services.chat.orchestrator import _make_tool_interceptor

        captured: dict = {}

        def _cb(tool_name, event_type_str, event_data, result_id=None, params=None, result_str=None, full_payload=None):
            captured["result_id"] = result_id
            captured["payload"] = full_payload

        interceptor = _make_tool_interceptor(cache_callback=_cb)
        empty_financial = {
            "success": True,
            "report_type": "income_statement",
            "period": "Jun 2026",
            "columns": ["Account", "Amount"],
            "items": [],
            "total_rows": 0,
            "summary": {"total_revenue": 0, "net_income": 0},
        }
        sse_tuple, llm_str = interceptor("netsuite_financial_report", _result_str(empty_financial))
        assert sse_tuple is not None
        assert json.loads(llm_str)["result_id"] == "r1"
        assert captured["result_id"] == "r1"
        assert captured["payload"] is not None, (
            "an empty-but-successful financial report's stamped id must resolve, not dangle"
        )
        assert captured["payload"]["row_count"] == 0


# -- ns_runReport hierarchical reportData (external Oracle NetSuite MCP) --


# The external Oracle NetSuite MCP ``ns_runReport`` returns a hierarchical
# ``{"reportData": {...}}`` payload — NOT the local financial tool's
# {success, items, summary} shape. Entries are keyed by stringified ints, each a
# dict carrying label/value + summary/detailLineValues. _extract_report_data_as_table
# flattens it to columns ["row", "account", "amount"].
SAMPLE_RUNREPORT_REPORTDATA = {
    "reportData": {
        "0": {"label": "Cash", "isDetailLine": True, "summaryLineValues": [{"Amount": 11500000}]},
        "1": {"value": "Accounts Receivable", "isDetailLine": True, "detailLineValues": [{"amount": 23200000}]},
        "2": {"label": "Net Income", "isDetailLine": False, "summaryLineValues": [{"Amount": 5200000}]},
    }
}


class TestInterceptRunReportReportData:
    """``ns_runReport`` (external Oracle NetSuite MCP financial reports) returns a
    hierarchical ``{"reportData": {...}}`` payload. ``extract_result_payload`` Path 2
    flattens it (so it gets PERSISTED to ``ChatMessage.tool_calls[].result_payload``),
    but the intercept's financial branch used to bail on the missing ``success`` key,
    emitting NO event → NO result_id → NO in-turn sidecar. A SAME-TURN
    ``report.compose`` then KeyError'd r1/r2 and rendered 'Data unavailable' error
    sections (no chart/table) — the prod failure (report 16625be0). The intercept MUST
    flatten reportData and emit a stamped data event so the sidecar id is written."""

    def test_reportdata_emits_stamped_data_event(self):
        result_str = _result_str(SAMPLE_RUNREPORT_REPORTDATA)
        event_type, sse_event, condensed = _intercept_tool_result(
            "ext__abc123def__ns_runReport", result_str, result_id="r1"
        )
        assert event_type == "data_table"  # a stamped data event (in _STAMPED_DATA_EVENTS)
        assert sse_event is not None
        # ["account", "amount"] — the row-type marker column is dropped so a chart's
        # x-axis (= first column) is the account name, not "detail"/"section".
        assert sse_event["columns"] == ["account", "amount"]
        assert len(sse_event["rows"]) == 3
        # The model must SEE the id so report.compose can reference it.
        assert json.loads(condensed)["result_id"] == "r1"
        assert sse_event["result_id"] == "r1"

    def test_reportdata_condensed_withholds_full_rows(self):
        """No-LLM-numbers parity with the data_table path: the condensed string must
        not dump the full flattened table — only a preview + a 'do not rebuild' note."""
        result_str = _result_str(SAMPLE_RUNREPORT_REPORTDATA)
        _, _, condensed = _intercept_tool_result("ext__abc__ns_runReport", result_str)
        parsed = json.loads(condensed)
        assert "rows" not in parsed
        assert "note" in parsed
        assert parsed["row_count"] == 3

    def test_empty_reportdata_is_noop(self):
        """An empty reportData has nothing to flatten → no stamped event (nothing to
        resolve), mirroring the empty-suiteql / 'other'-tool no-op paths."""
        empty = _result_str({"reportData": {}})
        event_type, sse_event, returned = _intercept_tool_result("ext__abc__ns_runReport", empty)
        assert event_type is None
        assert sse_event is None
        assert returned == empty

    def test_reportdata_invalid_json_is_noop(self):
        event_type, sse_event, returned = _intercept_tool_result("ext__abc__ns_runReport", "Not JSON")
        assert event_type is None
        assert sse_event is None
        assert returned == "Not JSON"

    def test_local_financial_items_shape_still_works(self):
        """Regression guard: the local {success, items, summary} financial shape must
        still emit financial_report (the reportData branch must not shadow it)."""
        result_str = _result_str(SAMPLE_FINANCIAL_RESULT)
        event_type, sse_event, _ = _intercept_tool_result("netsuite.financial_report", result_str)
        assert event_type == "financial_report"
        assert sse_event["rows"] == SAMPLE_FINANCIAL_RESULT["items"]

    def test_error_reportdata_is_noop_and_keeps_parity(self):
        """T2-gate #1/#2: an ERROR payload that still carries a reportData dict must
        NOT be intercepted as a data_table — the persistence path
        (extract_result_payload) bails on error, so the intercept MUST too, or it
        emits a bogus table for a FAILED report and the persist/intercept parity this
        PR exists to uphold breaks in the error direction."""
        from app.services.chat.tool_call_results import extract_result_payload

        err = {"error": True, "message": "Report failed", "reportData": SAMPLE_RUNREPORT_REPORTDATA["reportData"]}
        result_str = _result_str(err)
        event_type, sse_event, returned = _intercept_tool_result("ext__abc__ns_runReport", result_str)
        assert event_type is None
        assert sse_event is None
        assert returned == result_str
        # Parity: extract_result_payload also rejects it → both agree (no split).
        assert extract_result_payload("ext__abc__ns_runReport", {}, result_str) is None

    def test_string_error_reportdata_is_noop(self):
        err = {"error": "permission denied", "reportData": SAMPLE_RUNREPORT_REPORTDATA["reportData"]}
        result_str = _result_str(err)
        event_type, sse_event, _ = _intercept_tool_result("ext__abc__ns_runReport", result_str)
        assert event_type is None
        assert sse_event is None

    def test_success_false_reportdata_is_noop_and_not_persisted(self):
        """T2-gate re-review #1 (major): a FAILED report shaped {success: false,
        reportData: {...}} with NO `error` key must be rejected by BOTH the intercept
        and the persistence path — never a rendered/persisted table for a failed
        report. Both guard on `success is not False` (parity preserved, both safe)."""
        from app.services.chat.tool_call_results import extract_result_payload

        payload = {
            "success": False,
            "message": "Report failed",
            "reportData": SAMPLE_RUNREPORT_REPORTDATA["reportData"],
        }
        result_str = _result_str(payload)
        event_type, sse_event, returned = _intercept_tool_result("ext__abc__ns_runReport", result_str)
        assert event_type is None
        assert sse_event is None
        assert returned == result_str
        assert extract_result_payload("ext__abc__ns_runReport", {}, result_str) is None

    def test_empty_reportdata_with_bare_items_no_success_is_noop_and_not_persisted(self):
        """T2-gate re-review #1 (major) + #6 (test gap): empty reportData + a co-present
        items/data list WITHOUT `success` must be a no-op on BOTH paths. The intercept
        emits no stamped event (reportData flattens to None → success gate → None), so
        persistence must NOT freeze a payload either — a persisted-but-unstamped id
        drifts the cross-turn r-id numbering (count_payload_bearing_tool_calls counts a
        phantom the visible stamped sequence never had)."""
        from app.services.chat.tool_call_results import extract_result_payload

        for key in ("items", "data"):  # local 'items' AND external-MCP 'data'
            payload = {"reportData": {}, key: [{"acct": "Cash", "amt": 100}]}
            result_str = _result_str(payload)
            event_type, sse_event, _ = _intercept_tool_result("ext__abc__ns_runReport", result_str)
            assert event_type is None, f"empty reportData + {key} must not stamp an event"
            assert sse_event is None
            assert extract_result_payload("ext__abc__ns_runReport", {}, result_str) is None, (
                f"empty reportData + {key} must not persist a payload (no dangling id)"
            )

    def test_empty_reportdata_with_items_and_success_persists_and_stamps(self):
        """The success-gated counterpart: empty reportData + items WITH success:true is
        a real financial result — the intercept stamps a financial_report, so extract
        MUST persist too (parity in the OTHER direction)."""
        from app.services.chat.tool_call_results import extract_result_payload

        payload = {"reportData": {}, "success": True, "items": [{"acct": "Cash", "amt": 100}]}
        result_str = _result_str(payload)
        event_type, _, _ = _intercept_tool_result("ext__abc__ns_runReport", result_str)
        assert event_type == "financial_report"
        assert extract_result_payload("ext__abc__ns_runReport", {}, result_str) is not None

    def test_financial_summary_shape_wins_over_reportdata_in_both_paths(self):
        """T2-gate re-review #3 (branch-order parity): when a payload carries BOTH the
        financial Path-0 shape (success+summary+report_type) AND a non-empty reportData,
        BOTH the intercept and extract_result_payload must resolve the FINANCIAL shape
        first — never a reportData data_table on one side and a financial table on the
        other (extract checks Path 0 before Path 2; the intercept must match)."""
        from app.services.chat.tool_call_results import extract_result_payload

        payload = {
            "success": True,
            "report_type": "balance_sheet",
            "period": "Jun 2026",
            "columns": ["Account", "Amount"],
            "items": [{"account": "Cash", "amount": 100}],
            "summary": {"total": 100},
            "reportData": SAMPLE_RUNREPORT_REPORTDATA["reportData"],  # non-empty, co-present
        }
        result_str = _result_str(payload)
        event_type, sse_event, _ = _intercept_tool_result("ext__abc__ns_runReport", result_str)
        assert event_type == "financial_report", "financial Path-0 shape must win over co-present reportData"
        assert sse_event["rows"] == payload["items"]
        persisted = extract_result_payload("ext__abc__ns_runReport", {}, result_str)
        assert persisted["columns"] == ["Account", "Amount"], "extract must also resolve the financial table"

    def test_reportdata_query_is_blank_to_suppress_suiteql_export(self):
        """T2-gate #7: reportData is NOT a SuiteQL source. A non-empty ``query`` makes
        the FE offer a 'Download CSV' that re-runs the string as SuiteQL (→ HTTP 400)
        and mislabels the disclosure as a 'SuiteQL Query'. The query must be blank."""
        _, sse_event, _ = _intercept_tool_result("ext__abc__ns_runReport", _result_str(SAMPLE_RUNREPORT_REPORTDATA))
        assert sse_event["query"] == ""

    def test_large_reportdata_caps_rows_and_marks_truncated(self):
        """T2-gate #4/#6/#9: >2000 flattened rows must be capped to 2000 + truncated=True
        in BOTH the SSE event and the condensed string, matching the persisted/sidecar
        payload (extract_result_payload caps at MAX_STORED_PAYLOAD_ROWS). The TRUE
        row_count is preserved so the FE shows 'first 2000 of N'."""
        from app.services.chat.tool_call_results import MAX_STORED_PAYLOAD_ROWS

        n = MAX_STORED_PAYLOAD_ROWS + 500
        big = {
            "reportData": {
                str(i): {"label": f"Acct {i}", "isDetailLine": True, "summaryLineValues": [{"Amount": i}]}
                for i in range(n)
            }
        }
        event_type, sse_event, condensed = _intercept_tool_result("ext__abc__ns_runReport", _result_str(big))
        assert event_type == "data_table"
        assert sse_event["truncated"] is True
        assert len(sse_event["rows"]) == MAX_STORED_PAYLOAD_ROWS
        assert sse_event["row_count"] == n  # TRUE pre-cap count preserved
        parsed = json.loads(condensed)
        assert parsed["truncated"] is True
        assert parsed["row_count"] == n

    def test_empty_reportdata_with_financial_items_falls_through(self):
        """T2-gate #12: when reportData flattens to None but the payload ALSO carries
        the local {success, items} financial shape, the intercept must FALL THROUGH to
        the items path (parity with extract_result_payload, which would persist the
        items) — not short-circuit to a no-op and dangle a persisted-but-unstamped id."""
        payload = {
            "reportData": {},  # flattens to None
            "success": True,
            "report_type": "income_statement",
            "period": "Jun 2026",
            "columns": ["Account", "Amount"],
            "items": [{"account": "Revenue", "amount": 100}],
            "summary": {"net_income": 100},
        }
        event_type, sse_event, _ = _intercept_tool_result("netsuite.financial_report", _result_str(payload))
        assert event_type == "financial_report"
        assert sse_event["rows"] == payload["items"]

    def test_zero_amount_balance_is_preserved_not_dropped(self):
        """T2-gate #3/#8 (falsy-zero): a legitimate zero balance ({"Amount": 0}) must
        flatten to 0, not None — the `x or y` idiom silently dropped real $0 lines
        (common in P&L / balance sheets). Tests the shared flatten helper directly."""
        from app.services.chat.tool_call_results import _extract_report_data_as_table

        result = _extract_report_data_as_table(
            {"0": {"label": "Cash", "isDetailLine": True, "summaryLineValues": [{"Amount": 0}]}}
        )
        assert result is not None
        _cols, rows, _meta = result
        assert rows == [["Cash", 0]]

    def test_null_capital_amount_falls_through_to_lowercase(self):
        """T2-gate re-review #4: the falsy-zero fix must still cross-fall to the
        lowercase `amount` when capital `Amount` is present-but-NULL — preserve 0,
        fall through on None ({"Amount": null, "amount": 5} → 5, not None)."""
        from app.services.chat.tool_call_results import _extract_report_data_as_table

        result = _extract_report_data_as_table(
            {"0": {"label": "AR", "isDetailLine": True, "summaryLineValues": [{"Amount": None, "amount": 5}]}}
        )
        assert result is not None
        _cols, rows, _meta = result
        assert rows == [["AR", 5]]

    def test_flatten_keeps_every_amount_bearing_row_drops_only_truly_empty(self):
        """A financial surface must NEVER silently drop a figure (T2-gate major): a
        value-based 'duplicate' dedup would drop two genuinely distinct lines that
        coincide in amount (e.g. two $0 balance-sheet lines, two equal expense lines),
        so it is gone. Keep every row with a label OR a real amount (incl. $0 and a
        blank-label line that repeats the prior amount); drop only a truly-empty row.
        No hardcoded 'Financial Row' drop either (a tenant may name a real line that)."""
        from app.services.chat.tool_call_results import _extract_report_data_as_table

        rd = {
            "0": {"label": "Rent", "isDetailLine": True, "detailLineValues": [{"amount": 50000}]},
            "1": {"label": "", "isDetailLine": True, "detailLineValues": [{"amount": 50000}]},  # same amt → KEPT
            "2": {"label": "", "isDetailLine": True, "detailLineValues": [{"amount": 0}]},  # $0 figure → KEPT
            "3": {"label": "Financial Row", "isDetailLine": False, "summaryLineValues": [{"Amount": 100}]},  # KEPT
            "4": {"label": "", "isDetailLine": True, "detailLineValues": [{}]},  # no label, no amount → drop
        }
        result = _extract_report_data_as_table(rd)
        assert result is not None
        cols, rows, _meta = result
        assert cols == ["account", "amount"]
        assert rows == [["Rent", 50000], ["", 50000], ["", 0], ["Financial Row", 100]]

    def test_reportdata_payload_tags_amount_column_as_currency(self):
        """The reportData payload tags its 'amount' column as currency so the report
        renderer accounting-formats ONLY that column (not a generic numeric column)."""
        from app.services.chat.tool_call_results import extract_result_payload

        payload = extract_result_payload("ext__abc__ns_runReport", {}, _result_str(SAMPLE_RUNREPORT_REPORTDATA))
        assert payload["currency_columns"] == ["amount"]

    def test_persist_and_intercept_derive_identical_table_via_shared_helper(self):
        """T2-gate re-review #2: the persistence path (extract_result_payload Path 2)
        and the in-turn intercept derive the reportData table through ONE shared helper
        (report_data_to_capped_table), so columns/rows/row_count/truncated are
        byte-identical — parity is STRUCTURAL, not hand-maintained in two places."""
        from app.services.chat.tool_call_results import extract_result_payload, report_data_to_capped_table

        rd = _result_str(SAMPLE_RUNREPORT_REPORTDATA)
        columns, rows, _line_meta, row_count, truncated = report_data_to_capped_table(
            SAMPLE_RUNREPORT_REPORTDATA["reportData"]
        )
        persisted = extract_result_payload("ext__abc__ns_runReport", {}, rd)
        _, sse_event, _ = _intercept_tool_result("ext__abc__ns_runReport", rd)
        assert persisted["columns"] == sse_event["columns"] == columns
        assert persisted["rows"] == sse_event["rows"] == rows
        assert persisted["row_count"] == sse_event["row_count"] == row_count
        assert persisted["truncated"] == sse_event["truncated"] == truncated


class TestRunReportSlotAndSidecar:
    """The single-id-assignment invariant for reportData: ``_make_tool_interceptor``
    must assign a result_id AND hand the sidecar callback a non-None payload — they
    must agree, exactly as for the {data:[...]} and {success,items} shapes. This is
    the precise reproduction of the prod bug: pre-fix, reportData got a PERSISTED
    payload but NO result_id and NO sidecar, so a same-turn compose KeyError'd."""

    def test_reportdata_gets_id_and_sidecar_payload(self):
        from app.services.chat.orchestrator import _make_tool_interceptor

        captured: dict = {}

        def _cb(tool_name, event_type_str, event_data, result_id=None, params=None, result_str=None, full_payload=None):
            captured["result_id"] = result_id
            captured["payload"] = full_payload

        interceptor = _make_tool_interceptor(cache_callback=_cb)
        sse_tuple, llm_str = interceptor("ext__abc__ns_runReport", json.dumps(SAMPLE_RUNREPORT_REPORTDATA))
        assert sse_tuple is not None
        event_type, sse_event = sse_tuple
        assert event_type == "data_table"
        assert json.loads(llm_str)["result_id"] == "r1"
        # ... AND the sidecar callback got a non-None payload keyed by the same id.
        assert captured["result_id"] == "r1"
        assert captured["payload"] is not None, "pre-fix this was None → no sidecar → same-turn KeyError"
        assert captured["payload"]["kind"] == "table"
        assert len(captured["payload"]["rows"]) == 3

    def test_extract_and_intercept_agree_for_reportdata(self):
        """Close the CLASS, not just the case: for any tool that PERSISTS a payload
        (is_stamped_data_tool AND extract_result_payload non-None), the intercept MUST
        emit a stamped data event so the in-turn sidecar id is written. reportData was
        the shape that violated this parity — persisted but never stamped."""
        from app.services.chat.orchestrator import _STAMPED_DATA_EVENTS
        from app.services.chat.tool_call_results import extract_result_payload, is_stamped_data_tool

        tool = "ext__abc__ns_runReport"
        result_str = _result_str(SAMPLE_RUNREPORT_REPORTDATA)
        persisted = is_stamped_data_tool(tool) and extract_result_payload(tool, {}, result_str) is not None
        assert persisted, "precondition: reportData IS persisted by the stamped-data-tool path"
        event_type, _, _ = _intercept_tool_result(tool, result_str)
        assert event_type in _STAMPED_DATA_EVENTS, (
            "a persisted payload MUST correspond to a stamped intercept event, else the "
            "in-turn sidecar id is never written and a same-turn report.compose KeyErrors"
        )


class TestSameTurnRunReportComposeIntegration:
    """END-TO-END reproduction of the prod failure (report 16625be0): an ns_runReport
    (reportData) result intercepted THIS turn must be resolvable by a SAME-TURN
    report.compose via the in-turn sidecar — producing a rendered table/chart, NOT a
    'Data unavailable' error section. The persisted-message fallback cannot see the
    un-committed current turn, so the sidecar is the only source."""

    @pytest.fixture
    def mock_redis(self):
        store: dict = {}

        class FakeRedis:
            def hset(self, key, field, value):
                store.setdefault(key, {})[field] = value

            def hget(self, key, field):
                return store.get(key, {}).get(field)

            def hgetall(self, key):
                return store.get(key, {})

            def hlen(self, key):
                return len(store.get(key, {}))

            def hdel(self, key, field):
                store.get(key, {}).pop(field, None)

            def expire(self, key, ttl):
                pass

        with patch("app.services.chat.result_cache._get_redis", return_value=FakeRedis()):
            yield store

    def test_same_turn_reportdata_renders_table_and_chart_not_error(self, mock_redis):
        from app.services.chat.orchestrator import _make_tool_interceptor
        from app.services.chat.result_cache import cache_full_payload, get_full_payload
        from app.services.report.report_service import assemble_spec

        conv_id = "conv-same-turn-runreport"

        # Mirror the orchestrator's _on_tool_intercepted sidecar write exactly.
        def _cb(tool_name, event_type_str, event_data, result_id=None, params=None, result_str=None, full_payload=None):
            if result_id and full_payload is not None:
                cache_full_payload(conv_id, result_id, full_payload)

        interceptor = _make_tool_interceptor(cache_callback=_cb)
        # The data tool runs THIS turn (its assistant ChatMessage is NOT persisted yet).
        interceptor("ext__abc__ns_runReport", json.dumps(SAMPLE_RUNREPORT_REPORTDATA))

        # report.compose's PRIMARY resolver: sidecar-first; the persisted fallback
        # cannot see this un-committed turn, so a sidecar miss is a hard KeyError —
        # the exact prod failure mode that produced the 'Data unavailable' sections.
        def resolver(rid):
            cached = get_full_payload(conv_id, rid)
            if cached is None:
                raise KeyError(rid)
            return cached

        spec = assemble_spec(
            "Cash-Flow Report",
            [
                {"type": "table", "result_id": "r1"},
                {"type": "chart", "result_id": "r1", "chart_type": "bar"},
            ],
            resolver,
        )
        table, chart = spec["sections"]
        assert table["type"] == "table" and table["rows"], "reportData must resolve into a table, not an error"
        assert chart["type"] == "chart" and chart.get("svg"), "reportData must resolve into a chart, not an error"
        # The chart x-axis must be the ACCOUNT name, not the dropped row-type marker:
        # the SVG x-labels carry the account names and never "detail"/"section".
        svg = chart["svg"]
        assert "Cash" in svg and "Accounts Receivable" in svg, "chart x-axis must label bars by account name"
        # Phase 4: an explicit chart over a statement payload charts the LEAF drivers
        # only — "Net Income" is a summary line (isDetailLine: False) and must NOT be a
        # bar next to the detail lines (a subtotal bar double-counts its own details).
        assert "Net Income" not in svg, "summary lines are excluded from driver charts"
        assert "detail" not in svg and "section" not in svg, "row-type marker must not be the chart x-axis"


class TestInterceptorArityDispatch:
    """Finding #3: ``_call_tool_result_interceptor`` must decide arity ONCE via
    ``inspect.signature`` and call with the right shape — a REAL TypeError raised
    INSIDE the interceptor body must PROPAGATE, not be silently swallowed and
    retried with fewer args (which would mask bugs and double-run side effects)."""

    def test_internal_typeerror_propagates(self):
        from app.services.chat.agents.base_agent import _call_tool_result_interceptor

        calls: list = []

        def four_arg(tool_name, result_str, params=None, full_result_str=None):
            calls.append(tool_name)
            raise TypeError("boom inside the interceptor body")

        with pytest.raises(TypeError, match="boom inside"):
            _call_tool_result_interceptor(four_arg, "netsuite_suiteql", "capped", {}, "full")
        # The interceptor must have been invoked EXACTLY once (no arity retry).
        assert calls == ["netsuite_suiteql"]

    def test_three_arg_interceptor_dispatched_correctly(self):
        from app.services.chat.agents.base_agent import _call_tool_result_interceptor

        seen: dict = {}

        def three_arg(tool_name, result_str, params=None):
            seen["params"] = params
            return None, result_str

        out = _call_tool_result_interceptor(three_arg, "x", "capped", {"q": 1}, "full")
        assert seen["params"] == {"q": 1}
        assert out == (None, "capped")

    def test_three_arg_internal_typeerror_propagates(self):
        from app.services.chat.agents.base_agent import _call_tool_result_interceptor

        def three_arg(tool_name, result_str, params=None):
            raise TypeError("boom inside 3-arg body")

        with pytest.raises(TypeError, match="boom inside 3-arg"):
            _call_tool_result_interceptor(three_arg, "x", "capped", {}, "full")
