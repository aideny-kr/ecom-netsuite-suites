"""T2 multi-angle gate (minor): the legacy single-agent path must preserve a metric's
source_kind in the persisted tool log, so _compute_source_pin_update can distinguish a
BigQuery metric from a SuiteQL/NetSuite one and not mis-pin the data source.

The legacy intercept site reassigned ``result_str`` to the CONDENSED metric string (which
strips source_kind) and built the tool-call log from it. The production UnifiedAgent path
logs the RAW result and condenses a SEPARATE LLM-facing copy; the fix mirrors that on the
legacy path. (No run_chat_turn legacy harness exists, so the wiring is asserted via static
source inspection — the same pattern used in test_orchestrator_pricing_intercept.py.)
"""

from __future__ import annotations

import inspect
import json

from app.services.chat.tool_call_results import build_tool_call_log_entry
from app.services.metrics.metric_compute import condense_metric_for_llm, metric_data_table


def _bq_metric_raw() -> str:
    return json.dumps(
        metric_data_table("Net Margin", 0.25, "percent", "this_month", "net_margin", source_kind="bigquery")
    )


def test_condensed_metric_drops_source_kind_but_raw_preserves_it():
    """The seam the legacy path depends on: condensing for the LLM (the exact function the
    interceptor calls) strips source_kind — so a tool log built from the condensed string
    loses it (the bug) — while a log built from the RAW result retains it (the fix)."""
    raw = _bq_metric_raw()

    condensed = condense_metric_for_llm(json.loads(raw))
    assert "source_kind" not in condensed
    condensed_entry = build_tool_call_log_entry(
        step=0, tool_name="metric_compute", params={}, result_str=condensed, duration_ms=1
    )
    assert "source_kind" not in (condensed_entry.get("result_payload") or {})

    raw_entry = build_tool_call_log_entry(step=0, tool_name="metric_compute", params={}, result_str=raw, duration_ms=1)
    assert (raw_entry.get("result_payload") or {}).get("source_kind") == "bigquery"


def test_legacy_intercept_site_logs_raw_result_not_condensed():
    """Static wiring guard. The legacy single-agent intercept site must capture the RAW
    result_str BEFORE _intercept_with_cache reassigns it to the condensed string, and build
    the persisted tool-call log from that raw value.

    Pre-fix the log was built from the post-intercept (condensed) result_str — stripping
    source_kind. Post-fix the raw capture is logged so source_kind survives."""
    from app.services.chat import orchestrator

    source = inspect.getsource(orchestrator)
    # Anchor on the stable lead comment of the legacy single-agent intercept site.
    idx = source.index("single-agent / legacy path")
    window = source[idx : idx + 3000]
    assert "_raw_result_str = result_str" in window, "legacy path must capture the raw result before interception"
    assert "result_str=_raw_result_str" in window, "legacy path must log the raw result, not the condensed string"
