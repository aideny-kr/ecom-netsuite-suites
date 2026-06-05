"""TDD tests for NEW-4: value-absent scoring helper AND runner wiring.

Part A — the scoring helper (assert_computed_value_absent): verifies that
computed metric values do NOT leak into the model-visible answer text.

Part B (NEW-4 wiring) — the benchmark runner extraction + fail path:
verifies that _extract_computed_values_from_metric_tables correctly pulls
the Value cell from metric data_table payloads, and that _run_single_case
hard-fails (score=0.0, success=False) when a value leaks into the answer.
These tests use fabricated case-result objects — no live agent run needed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services.benchmarks.agent_runner import AgentRunResult, _extract_metric_data_tables
from app.services.benchmarks.run_vs_mcp import (
    Case,
    CaseResult,
    SideScore,
    _extract_computed_values_from_metric_tables,
    _run_single_case,
)
from app.services.benchmarks.scorer import assert_computed_value_absent


class TestAssertComputedValueAbsent:
    """Tests for the value-absent scoring helper.

    Spec: returns True if none of the computed_values appear as substrings
    in answer_text; False if any do. Case-insensitive substring match.
    """

    def test_no_leak_passes(self):
        """Answer without the computed value should pass (return True)."""
        assert (
            assert_computed_value_absent(
                "Net margin is healthy this quarter.",
                ["12.5", "12.5%"],
            )
            is True
        )

    def test_exact_value_leak_fails(self):
        """Answer containing the exact numeric value must return False."""
        assert (
            assert_computed_value_absent(
                "Your net margin is 12.5% for Q1.",
                ["12.5"],
            )
            is False
        )

    def test_percent_value_leak_fails(self):
        """Percentage variant of the value leaking returns False."""
        assert (
            assert_computed_value_absent(
                "Net margin: 12.5% last quarter.",
                ["12.5", "12.5%"],
            )
            is False
        )

    def test_empty_computed_values_always_passes(self):
        """With no computed values to guard, always returns True (vacuous)."""
        assert (
            assert_computed_value_absent(
                "Net margin looks great.",
                [],
            )
            is True
        )

    def test_empty_answer_with_values_passes(self):
        """Empty answer cannot contain a leak — should pass."""
        assert assert_computed_value_absent("", ["12.5"]) is True

    def test_multiple_values_one_leaks_fails(self):
        """If ANY computed value is in the answer, should fail."""
        assert (
            assert_computed_value_absent(
                "Revenue was $5,000,000 last quarter.",
                ["12.5", "5,000,000"],
            )
            is False
        )

    def test_multiple_values_none_leak_passes(self):
        """If NO computed value is in the answer, should pass."""
        assert (
            assert_computed_value_absent(
                "Performance was strong last quarter.",
                ["12.5", "5000000"],
            )
            is True
        )

    def test_value_in_unrelated_context_still_fails(self):
        """Even if the number appears in an unrelated context, it leaks."""
        assert (
            assert_computed_value_absent(
                "We have 12.5 engineers on the team and margin is healthy.",
                ["12.5"],
            )
            is False
        )

    def test_case_insensitive_match(self):
        """Substring match should be case-insensitive (for string values)."""
        assert (
            assert_computed_value_absent(
                "margin is Q1",
                ["q1"],
            )
            is False
        )

    def test_partial_number_does_not_match(self):
        """'125' should NOT match when computed value is '12.5' — exact substring only."""
        assert (
            assert_computed_value_absent(
                "The count was 125 items.",
                ["12.5"],
            )
            is True
        )


# ---------------------------------------------------------------------------
# NEW-4 Part B — extraction helper: _extract_metric_data_tables
# ---------------------------------------------------------------------------


class TestExtractMetricDataTables:
    """Tests for agent_runner._extract_metric_data_tables.

    Verifies that metric data_table payloads (columns starting with
    ["Metric","Value","Unit","Period"]) are correctly pulled from a
    tool_calls_log, and that non-metric payloads are ignored.
    """

    _METRIC_PAYLOAD = {
        "kind": "table",
        "columns": ["Metric", "Value", "Unit", "Period"],
        "rows": [["Net Margin", 12.5, "%", "Q1 2025"]],
        "row_count": 1,
        "query": "net_margin",
        "truncated": False,
        "limit": 1,
    }

    def _log_entry(self, payload: dict | None, tool: str = "metric_compute") -> dict:
        entry: dict = {"step": 0, "tool": tool, "params": {}, "result_summary": {}}
        if payload is not None:
            entry["result_payload"] = payload
        return entry

    def test_extracts_metric_payload(self):
        log = [self._log_entry(self._METRIC_PAYLOAD)]
        result = _extract_metric_data_tables(log)
        assert len(result) == 1
        assert result[0]["rows"][0][1] == 12.5

    def test_ignores_non_metric_payload(self):
        non_metric = {
            "kind": "table",
            "columns": ["country", "revenue"],
            "rows": [["US", 100000]],
        }
        log = [self._log_entry(non_metric, tool="netsuite_suiteql")]
        result = _extract_metric_data_tables(log)
        assert result == []

    def test_ignores_entry_without_result_payload(self):
        log = [self._log_entry(None)]
        result = _extract_metric_data_tables(log)
        assert result == []

    def test_collects_multiple_metric_payloads(self):
        payload2 = dict(self._METRIC_PAYLOAD)
        payload2 = {**self._METRIC_PAYLOAD, "rows": [["Gross Margin", 45.0, "%", "Q1 2025"]]}
        log = [
            self._log_entry(self._METRIC_PAYLOAD),
            self._log_entry(payload2),
        ]
        result = _extract_metric_data_tables(log)
        assert len(result) == 2

    def test_empty_log_returns_empty(self):
        assert _extract_metric_data_tables([]) == []

    def test_none_log_returns_empty(self):
        assert _extract_metric_data_tables(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# NEW-4 Part B — value extraction: _extract_computed_values_from_metric_tables
# ---------------------------------------------------------------------------


class TestExtractComputedValuesFromMetricTables:
    """Tests for run_vs_mcp._extract_computed_values_from_metric_tables.

    Verifies that the Value cell (index 1) is extracted as a string from
    metric data_table payloads, that both raw and str() forms are included
    when they differ, and that non-metric payloads are skipped.
    """

    _METRIC_TABLE = {
        "kind": "table",
        "columns": ["Metric", "Value", "Unit", "Period"],
        "rows": [["Net Margin", 12.5, "%", "Q1 2025"]],
        "row_count": 1,
        "query": "net_margin",
        "truncated": False,
        "limit": 1,
    }

    def test_extracts_value_as_string(self):
        values = _extract_computed_values_from_metric_tables([self._METRIC_TABLE])
        # Should contain "12.5" (from str(12.5))
        assert "12.5" in values

    def test_empty_tables_returns_empty(self):
        assert _extract_computed_values_from_metric_tables([]) == []

    def test_none_tables_returns_empty(self):
        assert _extract_computed_values_from_metric_tables(None) == []  # type: ignore[arg-type]

    def test_non_metric_columns_skipped(self):
        bad = {
            "columns": ["country", "revenue"],
            "rows": [["US", 999999]],
        }
        assert _extract_computed_values_from_metric_tables([bad]) == []

    def test_string_value_extracted(self):
        table = {
            "columns": ["Metric", "Value", "Unit", "Period"],
            "rows": [["Net Margin", "12.5%", "%", "Q1 2025"]],
        }
        values = _extract_computed_values_from_metric_tables([table])
        assert "12.5%" in values

    def test_multiple_rows_all_extracted(self):
        table = {
            "columns": ["Metric", "Value", "Unit", "Period"],
            "rows": [
                ["Net Margin", 12.5, "%", "Q1"],
                ["Gross Margin", 45.0, "%", "Q1"],
            ],
        }
        values = _extract_computed_values_from_metric_tables([table])
        assert "12.5" in values
        assert "45.0" in values


# ---------------------------------------------------------------------------
# NEW-4 Part B — full fail path: _run_single_case with computed_value_absent
# ---------------------------------------------------------------------------


def _make_metric_case(*, computed_value_absent: bool = True) -> Case:
    """Fabricate a Case that opts into the value-absent invariant."""
    return Case(
        case_id="test_metric_case",
        query="what's our net margin last quarter?",
        expected_answer_contains=["net margin"],
        expected_tools=["metric_compute"],
        expected_accuracy=0.7,
        max_cost=0.50,
        max_latency_ms=120_000,
        tags=["metric"],
        notes="",
        baseline_expected_tools=[],
        baseline_expected_accuracy=0.5,
        computed_value_absent=computed_value_absent,
    )


def _make_agent_run_result(
    *,
    answer_text: str,
    metric_value,
    success: bool = True,
) -> AgentRunResult:
    """Fabricate an AgentRunResult with a metric data_table payload."""
    metric_table = {
        "kind": "table",
        "columns": ["Metric", "Value", "Unit", "Period"],
        "rows": [["Net Margin", metric_value, "%", "Q1 2025"]],
        "row_count": 1,
        "query": "net_margin",
        "truncated": False,
        "limit": 1,
    }
    return AgentRunResult(
        answer_text=answer_text,
        tool_calls=[{"name": "metric_compute", "input": {}, "result_preview": ""}],
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.001,
        latency_ms=3000,
        success=success,
        error=None,
        metric_data_tables=[metric_table],
    )


class TestRunSingleCaseValueAbsent:
    """Integration tests for the NEW-4 value-absent wiring in _run_single_case.

    Uses fabricated AgentRunResult objects — no live agent run, no DB.
    Patches run_agent in the agent_runner module (where _run_single_case
    imports it lazily via `from app.services.benchmarks.agent_runner import
    run_agent`) and _score_answer to return a deterministic substring score.
    """

    def _run(self, case: Case, agent_result: AgentRunResult) -> CaseResult:
        """Run _run_single_case with patched run_agent + _score_answer."""

        async def _async():
            # _run_single_case does a lazy local import:
            #   from app.services.benchmarks.agent_runner import run_agent
            # Python resolves this via sys.modules, so patching the attribute
            # on the already-loaded agent_runner module is the right target.
            with (
                patch(
                    "app.services.benchmarks.agent_runner.run_agent",
                    new=AsyncMock(return_value=agent_result),
                ),
                # _score_answer calls llm_judge or substring; patch it to
                # return a fixed substring-based score (no API needed).
                patch(
                    "app.services.benchmarks.run_vs_mcp._score_answer",
                    new=AsyncMock(return_value=(1.0, "[test] keywords matched")),
                ),
            ):
                return await _run_single_case(
                    case=case,
                    tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                    agent_model="claude-sonnet-4-6",
                    baseline_model="claude-sonnet-4-6",
                    skip_baseline=True,
                    use_llm_judge=False,
                    db=None,
                )

        return asyncio.run(_async())

    def test_value_leak_hard_fails_case(self):
        """When the metric value leaks into the answer, the case must score 0.0
        and success=False with a clear violation reason."""
        case = _make_metric_case(computed_value_absent=True)
        # Answer contains the computed value "12.5" — invariant violated
        agent_result = _make_agent_run_result(
            answer_text="Your net margin is 12.5% for Q1 2025.",
            metric_value=12.5,
        )
        result = self._run(case, agent_result)

        assert result.ours.answer_acc == 0.0
        assert result.ours.success is False
        assert result.ours.error is not None
        assert "computed_value_absent violated" in result.ours.error
        assert "12.5" in result.ours.error

    def test_no_value_leak_does_not_fail(self):
        """When the answer does NOT contain the computed value, the invariant
        holds — the case is NOT hard-failed by the value-absent check."""
        case = _make_metric_case(computed_value_absent=True)
        # Answer references "net margin" but NOT the numeric value
        agent_result = _make_agent_run_result(
            answer_text="Net margin results are shown in the table above.",
            metric_value=12.5,
        )
        result = self._run(case, agent_result)

        # Should NOT be hard-failed by the value-absent check
        assert "computed_value_absent violated" not in (result.ours.error or "")
        # answer_acc must be the score from _score_answer (mocked to 1.0)
        assert result.ours.answer_acc == 1.0

    def test_computed_value_absent_false_skips_check(self):
        """When computed_value_absent is False, the check is skipped entirely
        even if the value is present in the answer."""
        case = _make_metric_case(computed_value_absent=False)
        # Value is present in the answer, but the check is opted-out
        agent_result = _make_agent_run_result(
            answer_text="Your net margin is 12.5% for Q1 2025.",
            metric_value=12.5,
        )
        result = self._run(case, agent_result)

        # Must NOT be failed by the value-absent check
        assert "computed_value_absent violated" not in (result.ours.error or "")
        assert result.ours.answer_acc == 1.0

    def test_no_metric_tables_skips_check(self):
        """When the agent result has no metric_data_tables (metric_compute was
        not called), the check is skipped — no false-positive hard-fail."""
        case = _make_metric_case(computed_value_absent=True)
        # No metric tables — metric_compute was not called
        agent_result = AgentRunResult(
            answer_text="Net margin is 12.5% (computed ad-hoc).",
            tool_calls=[],
            success=True,
            metric_data_tables=[],  # no metric payloads
        )
        result = self._run(case, agent_result)

        # With no metric tables, no computed values to check — not hard-failed
        assert "computed_value_absent violated" not in (result.ours.error or "")
        assert result.ours.answer_acc == 1.0
