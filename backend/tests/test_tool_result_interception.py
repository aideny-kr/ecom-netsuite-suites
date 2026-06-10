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
