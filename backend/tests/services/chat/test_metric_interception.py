# backend/tests/services/chat/test_metric_interception.py
"""The metric value must NOT leak into the LLM-facing condensed string.

A metric is a 1-row data_table whose whole point is the single computed number.
The anti-hallucination invariant ("never let the LLM present tool-computed
numbers") requires that number to reach the frontend via the SSE event_data
rows, while the condensed string the LLM sees carries commentary instructions
ONLY — no value, no rows, no rows_preview.

These tests assert the REAL invariant: the literal value string is present in
the SSE rows but absent from the condensed LLM string.
"""

import json

from app.services.chat.orchestrator import ContextNeed, _intercept_tool_result
from app.services.metrics.metric_compute import metric_data_table


def test_metric_value_reaches_frontend_but_not_llm_default_context():
    payload = metric_data_table("Net Margin", 0.2531, "percent", "last_quarter", "expr")

    event_type, sse_event_data, condensed = _intercept_tool_result("metric_compute", json.dumps(payload))

    # (a) routes through the data_table interception branch
    assert event_type == "data_table"

    # (b) the SSE rows the frontend renders DO carry the real value
    assert sse_event_data is not None
    flat = [cell for row in sse_event_data["rows"] for cell in row]
    assert 0.2531 in flat

    # (c) the LLM-facing condensed string must NOT contain the value
    assert "0.2531" not in condensed
    # ...and must not smuggle it via rows / rows_preview either
    parsed_condensed = json.loads(condensed)
    assert "rows" not in parsed_condensed
    assert "rows_preview" not in parsed_condensed
    # it should still tell the LLM the shape + the do-not-recompute instruction
    assert parsed_condensed.get("row_count") == 1
    assert parsed_condensed.get("columns") == ["Metric", "Value", "Unit", "Period"]
    assert "note" in parsed_condensed


def test_metric_value_not_leaked_in_full_context():
    # FULL context normally hands the LLM every row; suppression must still apply.
    payload = metric_data_table("Net Margin", 0.2531, "percent", "last_quarter", "expr")

    event_type, sse_event_data, condensed = _intercept_tool_result(
        "metric_compute", json.dumps(payload), ContextNeed.FULL
    )

    assert event_type == "data_table"
    flat = [cell for row in sse_event_data["rows"] for cell in row]
    assert 0.2531 in flat
    assert "0.2531" not in condensed
    assert "rows" not in json.loads(condensed)


def test_query_backed_metric_shape_also_suppressed():
    # A query-backed metric carries blessed_spec (a dict) as the query field and
    # still suppresses its single computed value from the LLM string.
    payload = metric_data_table(
        "Gross Revenue",
        1234567.89,
        "currency",
        "last_quarter",
        {"query": "SELECT SUM(amount) FROM ...", "dialect": "suiteql"},
    )

    event_type, sse_event_data, condensed = _intercept_tool_result("metric_compute", json.dumps(payload))

    assert event_type == "data_table"
    flat = [cell for row in sse_event_data["rows"] for cell in row]
    assert 1234567.89 in flat
    assert "1234567.89" not in condensed
    assert "rows" not in json.loads(condensed)


def test_suppress_flag_set_on_metric_data_table():
    payload = metric_data_table("Net Margin", 0.2531, "percent", "last_quarter", "expr")
    assert payload.get("suppress_llm_value") is True


def test_non_metric_data_table_is_unaffected():
    # A normal SuiteQL data_table (no suppress flag) must keep leaking its rows
    # into rows_preview exactly as before — suppression is opt-in only.
    payload = {
        "columns": ["order_id", "amount"],
        "rows": [["SO-1", 100.0], ["SO-2", 250.5]],
        "row_count": 2,
        "query": "SELECT order_id, amount FROM ...",
        "truncated": False,
    }

    event_type, sse_event_data, condensed = _intercept_tool_result("netsuite_suiteql", json.dumps(payload))

    assert event_type == "data_table"
    parsed_condensed = json.loads(condensed)
    # behavior byte-identical to before: rows_preview present, values visible
    assert "rows_preview" in parsed_condensed
    assert parsed_condensed["rows_preview"] == [["SO-1", 100.0], ["SO-2", 250.5]]
