"""TDD tests for Round 2 grill findings on the metric catalog.

NEW-1 (major) — SSE data_table event must include suppress_llm_value=True and source_kind
       for metric payloads, so the frontend can hide re-run/export/save-query affordances.

M4 (source-pin half) — BigQuery metrics must pin BigQuery, not NetSuite.
       _compute_source_pin_update must read source_kind from the metric result payload
       rather than treating metric_compute category ("data_table") as NetSuite.
"""

from __future__ import annotations

import json

import pytest

from app.services.chat.orchestrator import (
    _compute_source_pin_update,
    _intercept_tool_result,
)
from app.services.metrics.metric_compute import metric_data_table


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _metric_result_str(source_kind: str | None = "suiteql") -> str:
    """Build a JSON result_str for a metric_compute call."""
    payload = metric_data_table(
        display_name="Revenue",
        value=1_234_567.89,
        unit="USD",
        period_label="Q1 2026",
        query_label="revenue",
        definition_version=3,
        source_kind=source_kind,
    )
    return json.dumps(payload)


def _normal_suiteql_result_str() -> str:
    """Build a JSON result_str for a normal (non-metric) SuiteQL call."""
    return json.dumps(
        {
            "columns": ["tranid", "entity", "amount"],
            "rows": [["SO-1001", "Acme Corp", 5000.00]],
            "row_count": 1,
            "query": "SELECT tranid, entity, amount FROM transaction",
            "truncated": False,
        }
    )


# ---------------------------------------------------------------------------
# NEW-1: SSE event must carry suppress_llm_value + source_kind for metrics
# ---------------------------------------------------------------------------


class TestNew1MetricSSEEventCarriesFlags:
    """NEW-1: _intercept_tool_result must add suppress_llm_value and source_kind
    to sse_event_data when the payload is a suppressed metric.

    Frontend chat-stream.ts derives isMetric from suppress_llm_value to hide
    affordances (re-run / export / save-query) and expose the correct metric UI.
    """

    def test_metric_suiteql_sse_event_has_suppress_llm_value_true(self):
        result_str = _metric_result_str(source_kind="suiteql")
        event_type, sse_event, condensed = _intercept_tool_result("metric_compute", result_str)
        assert event_type == "data_table"
        assert sse_event is not None
        assert sse_event.get("suppress_llm_value") is True, (
            "sse_event_data must carry suppress_llm_value=True for metric payloads so the "
            "frontend can hide re-run/export/save-query affordances (NEW-1)."
        )

    def test_metric_bigquery_sse_event_has_suppress_llm_value_true(self):
        result_str = _metric_result_str(source_kind="bigquery")
        event_type, sse_event, condensed = _intercept_tool_result("metric_compute", result_str)
        assert event_type == "data_table"
        assert sse_event is not None
        assert sse_event.get("suppress_llm_value") is True

    def test_metric_sse_event_carries_source_kind(self):
        result_str = _metric_result_str(source_kind="bigquery")
        _, sse_event, _ = _intercept_tool_result("metric_compute", result_str)
        assert sse_event is not None
        assert sse_event.get("source_kind") == "bigquery", (
            "sse_event_data must pass through source_kind from the metric payload (NEW-1)."
        )

    def test_metric_suiteql_source_kind_passed_through(self):
        result_str = _metric_result_str(source_kind="suiteql")
        _, sse_event, _ = _intercept_tool_result("metric_compute", result_str)
        assert sse_event is not None
        assert sse_event.get("source_kind") == "suiteql"

    def test_metric_dotted_tool_name_also_gets_flag(self):
        """metric.compute (dotted form) must also get the suppress_llm_value flag."""
        result_str = _metric_result_str(source_kind="suiteql")
        event_type, sse_event, condensed = _intercept_tool_result("metric.compute", result_str)
        assert event_type == "data_table"
        assert sse_event is not None
        assert sse_event.get("suppress_llm_value") is True

    # --- Negative: normal SuiteQL data_table must NOT get suppress_llm_value ---

    def test_normal_suiteql_sse_event_has_no_suppress_llm_value(self):
        """Non-metric data_table results must be byte-identical (no suppress_llm_value added)."""
        result_str = _normal_suiteql_result_str()
        event_type, sse_event, condensed = _intercept_tool_result("netsuite_suiteql", result_str)
        assert event_type == "data_table"
        assert sse_event is not None
        assert "suppress_llm_value" not in sse_event, (
            "suppress_llm_value must NOT be added to normal (non-metric) data_table SSE events (NEW-1). "
            "Only metric payloads get the flag."
        )

    def test_normal_suiteql_sse_event_has_no_source_kind(self):
        """Non-metric data_table results must NOT gain a source_kind field."""
        result_str = _normal_suiteql_result_str()
        _, sse_event, _ = _intercept_tool_result("netsuite_suiteql", result_str)
        assert sse_event is not None
        assert "source_kind" not in sse_event, (
            "source_kind must NOT be added to normal (non-metric) data_table SSE events."
        )


# ---------------------------------------------------------------------------
# M4: _compute_source_pin_update must read source_kind from metric result payload
# ---------------------------------------------------------------------------


def _metric_log_entry(source_kind: str | None, tool_name: str = "metric_compute") -> dict:
    """Build a tool_calls_log entry that build_tool_call_log_entry would produce
    for a metric_compute call with the given source_kind."""
    from app.services.chat.tool_call_results import build_tool_call_log_entry

    result_str = _metric_result_str(source_kind=source_kind)
    return build_tool_call_log_entry(
        step=1,
        tool_name=tool_name,
        params={"metric_key": "revenue"},
        result_str=result_str,
        duration_ms=42,
    )


class TestM4MetricSourcePinRouting:
    """M4: A BigQuery-backed metric must pin BigQuery, not NetSuite.

    The bug: metric_compute is categorized as 'data_table' → used_ns=True.
    Fix: for metric tools, read source_kind from the result_payload / result_str.
    """

    def test_bigquery_metric_pins_bigquery(self):
        log = [_metric_log_entry(source_kind="bigquery")]
        result = _compute_source_pin_update(log)
        assert result == "bigquery", (
            f"Expected 'bigquery' for a BigQuery-backed metric, got {result!r}. "
            "M4: _compute_source_pin_update must read source_kind, not use tool category."
        )

    def test_suiteql_metric_pins_netsuite(self):
        log = [_metric_log_entry(source_kind="suiteql")]
        result = _compute_source_pin_update(log)
        assert result == "netsuite", f"Expected 'netsuite' for a SuiteQL-backed metric, got {result!r}."

    def test_expression_metric_leaves_pin(self):
        """Expression metrics are source-agnostic — must not set used_ns or used_bq."""
        log = [_metric_log_entry(source_kind="expression")]
        result = _compute_source_pin_update(log)
        assert result == "leave_pin", (
            f"Expected 'leave_pin' for an expression metric (source-agnostic), got {result!r}. "
            "Expression metrics must NOT pin either source."
        )

    def test_metric_without_source_kind_leaves_pin(self):
        """A metric payload missing source_kind (old data) must leave pin unchanged."""
        # metric_data_table with source_kind=None omits the key entirely
        log = [_metric_log_entry(source_kind=None)]
        result = _compute_source_pin_update(log)
        assert result == "leave_pin", f"Expected 'leave_pin' for a metric without source_kind, got {result!r}."

    def test_dotted_metric_compute_also_handled(self):
        """metric.compute (dotted form) must also use source_kind routing."""
        log = [_metric_log_entry(source_kind="bigquery", tool_name="metric.compute")]
        result = _compute_source_pin_update(log)
        assert result == "bigquery"

    # --- Non-metric data_table tools must still route via category (regression) ---

    def test_netsuite_suiteql_still_pins_netsuite(self):
        """Non-metric SuiteQL tool must continue to pin NetSuite."""
        from app.services.chat.tool_call_results import build_tool_call_log_entry

        entry = build_tool_call_log_entry(
            step=1,
            tool_name="netsuite_suiteql",
            params={"query": "SELECT tranid FROM transaction"},
            result_str=_normal_suiteql_result_str(),
            duration_ms=10,
        )
        result = _compute_source_pin_update([entry])
        assert result == "netsuite", f"Regression: netsuite_suiteql must still pin 'netsuite', got {result!r}."

    def test_bigquery_sql_still_pins_bigquery(self):
        """Plain bigquery_sql must continue to pin BigQuery."""
        log = [{"tool_name": "bigquery_sql"}]
        result = _compute_source_pin_update(log)
        assert result == "bigquery"

    def test_mixed_bq_metric_and_ns_suiteql_clears_pin(self):
        """BigQuery metric + NetSuite SuiteQL in same turn → mixed → clear pin."""
        from app.services.chat.tool_call_results import build_tool_call_log_entry

        bq_metric = _metric_log_entry(source_kind="bigquery")
        ns_entry = build_tool_call_log_entry(
            step=2,
            tool_name="netsuite_suiteql",
            params={"query": "SELECT tranid FROM transaction"},
            result_str=_normal_suiteql_result_str(),
            duration_ms=10,
        )
        result = _compute_source_pin_update([bq_metric, ns_entry])
        assert result is None, (
            f"Expected None (clear pin) for mixed BigQuery metric + NetSuite SuiteQL, got {result!r}."
        )
