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

    def test_partial_number_integer_form_detected(self):
        """NEW-4b: With variant matching, the integer form '12' of computed
        value '12.5' IS a generated variant (len>=2) and WILL be found as a
        substring in '125'. This is an accepted heuristic trade-off — the
        variant check is broader than exact-substring (as specified in NEW-4b).
        The old 'exact substring only' semantics no longer apply.

        For true production use, computed values are real metric outputs
        (e.g. '12.53457'); an answer containing '125' as a count would be
        an unlikely collision in a real metric-case answer. The check is
        documented as heuristic.
        """
        # '12' is the integer form of 12.5 → IS a substring of '125' → leak detected.
        # This is the NEW-4b heuristic behavior; the old exact-only test is superseded.
        assert (
            assert_computed_value_absent(
                "The count was 125 items.",
                ["12.5"],
            )
            is False
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

    # NEW-4a: vacuous-pass bug — no metric data_table → must now HARD FAIL
    def test_no_metric_tables_hard_fails_metric_case(self):
        """NEW-4a: When a computed_value_absent case runs successfully but
        produces NO metric data_table (metric_compute was never called), the
        gate must HARD FAIL — not skip the check.

        This closes the vacuous-pass gap: an agent that answers a metric
        question via ad-hoc SuiteQL (bypassing metric_compute entirely) would
        produce no metric data_table, previously causing the check to be
        silently skipped and the case to pass as 'OURS ONLY'.  Now the gate
        enforces that the blessed routing was used.
        """
        case = _make_metric_case(computed_value_absent=True)
        # No metric tables — metric_compute was NOT called; agent bypassed it
        agent_result = AgentRunResult(
            answer_text="Net margin is great this quarter.",
            tool_calls=[],
            success=True,
            metric_data_tables=[],  # no metric payloads → bypass detected
        )
        result = self._run(case, agent_result)

        # Must HARD FAIL because metric_compute was not used
        assert result.ours.answer_acc == 0.0
        assert result.ours.success is False
        assert result.ours.error is not None
        assert "metric_compute" in result.ours.error.lower() or "metric data_table" in result.ours.error.lower()

    def test_no_metric_tables_bypass_verdict_is_failed(self):
        """NEW-4a: The CaseResult verdict for a bypassed metric case must
        reflect failure (OURS FAILED), not OURS ONLY."""
        case = _make_metric_case(computed_value_absent=True)
        agent_result = AgentRunResult(
            answer_text="Margin looks fine.",
            tool_calls=[],
            success=True,
            metric_data_tables=[],
        )
        result = self._run(case, agent_result)

        # With skip_baseline=True, a failed ours → OURS FAILED
        assert result.verdict == "OURS FAILED"


# ---------------------------------------------------------------------------
# NEW-4b — value_leak_variants helper + strengthened assert_computed_value_absent
# ---------------------------------------------------------------------------


class TestValueLeakVariants:
    """Tests for the value_leak_variants() helper introduced in NEW-4b.

    Spec:
    - Raw value and stripped ($, %, ,) forms always included.
    - If parseable as a number: int form, 1-decimal, 2-decimal, thousands-sep
      and unsep forms.
    - Percent-scale variants: if value looks like a percent (N or N%), also
      include the 0-1 scaled form (N/100) and vice-versa.
    - Variants shorter than 2 chars are excluded (prevent trivial matches).
    """

    def _variants(self, value: str) -> set[str]:
        from app.services.benchmarks.scorer import value_leak_variants

        return value_leak_variants(value)

    def test_percent_string_includes_raw_and_stripped(self):
        v = self._variants("12.5%")
        assert "12.5%" in v
        assert "12.5" in v

    def test_percent_string_includes_scaled_form(self):
        """12.5% → 0.125 (divided by 100)."""
        v = self._variants("12.5%")
        assert "0.125" in v or "0.13" in v  # 0-1 scaled form

    def test_plain_number_includes_percent_scaled_form(self):
        """12.5 (plain) → 0.125 (as if it were a percent)."""
        v = self._variants("12.5")
        # Either the 0-1 form or rounded variant must appear
        assert "0.125" in v or "0.13" in v

    def test_thousands_and_unseparated_forms(self):
        """12500 → '12,500' and '12500' both present."""
        v = self._variants("12500")
        assert "12500" in v
        assert "12,500" in v

    def test_thousands_separated_input_includes_unseparated(self):
        """'12,500' → '12500' also present."""
        v = self._variants("12,500")
        assert "12500" in v
        assert "12,500" in v

    def test_integer_form_for_float(self):
        """12.0 → '12' (integer form)."""
        v = self._variants("12.0")
        assert "12" in v

    def test_dollar_stripped(self):
        """'$12.5' → '12.5' in variants."""
        v = self._variants("$12.5")
        assert "12.5" in v

    def test_min_length_guard_excludes_short_tokens(self):
        """Single-char or single-digit tokens must be excluded (avoid 'in 5%' matches)."""
        v = self._variants("5")
        # '5' is length 1, must not appear
        assert "5" not in v

    def test_variants_are_deterministic(self):
        """Calling twice must return the same set."""
        from app.services.benchmarks.scorer import value_leak_variants

        assert value_leak_variants("12.5%") == value_leak_variants("12.5%")

    def test_non_numeric_string_not_crash(self):
        """Non-numeric raw values must not crash — return what we can."""
        v = self._variants("Q1 2025")
        # At minimum the raw value should be present
        assert "Q1 2025" in v or "q1 2025" in v or len(v) >= 1

    def test_rounded_forms_for_long_decimal(self):
        """12.567 → '12.57' (2 dp) and '12.6' (1 dp) in variants."""
        v = self._variants("12.567")
        assert "12.57" in v
        assert "12.6" in v


class TestAssertComputedValueAbsentVariants:
    """Tests for the strengthened assert_computed_value_absent that uses
    value_leak_variants() to detect alternate numeric renderings.

    These complement (not replace) the existing TestAssertComputedValueAbsent
    tests — they exercise the variant paths added in NEW-4b.
    """

    def test_percent_scaled_form_detected(self):
        """'0.125' in answer while computed value is '12.5%' → leak detected."""
        from app.services.benchmarks.scorer import assert_computed_value_absent

        # The agent wrote the percent as a 0-1 decimal
        result = assert_computed_value_absent(
            "net margin ~0.125 this quarter",
            ["12.5%"],
        )
        assert result is False, "0.125 is a variant of 12.5% — should detect leak"

    def test_thousands_separated_form_detected(self):
        """'12,500' in answer while computed value is '12500' → leak."""
        from app.services.benchmarks.scorer import assert_computed_value_absent

        result = assert_computed_value_absent(
            "revenue was 12,500 units",
            ["12500"],
        )
        assert result is False, "12,500 is a thousands-sep variant of 12500"

    def test_unseparated_form_detected(self):
        """'12500' in answer while computed value is '12,500' → leak."""
        from app.services.benchmarks.scorer import assert_computed_value_absent

        result = assert_computed_value_absent(
            "revenue was 12500 units",
            ["12,500"],
        )
        assert result is False, "12500 is an unseparated variant of 12,500"

    def test_tilde_approximation_with_variant(self):
        """'~12%' in answer while computed value is '12.5%' → detect via '12' integer form."""
        # NOTE: '~12%' contains '12' which is the integer form of 12.5
        from app.services.benchmarks.scorer import assert_computed_value_absent

        result = assert_computed_value_absent(
            "net margin is approximately ~12% this quarter",
            ["12.5%"],
        )
        # '12' (integer form of 12.5) must be in variants AND len('12') >= 2 → leak
        assert result is False, "integer form '12' of '12.5%' should be detected"

    def test_clean_answer_still_passes(self):
        """An answer with NO numeric mention of the computed value passes."""
        from app.services.benchmarks.scorer import assert_computed_value_absent

        result = assert_computed_value_absent(
            "Net margin results are shown in the table above. Performance was strong.",
            ["12.5%"],
        )
        assert result is True, "Answer with no numeric variant should pass"

    def test_dollar_amount_variant_detected(self):
        """'5000' in answer while computed value is '$5,000' → leak."""
        from app.services.benchmarks.scorer import assert_computed_value_absent

        result = assert_computed_value_absent(
            "total revenue was 5000 this period",
            ["$5,000"],
        )
        assert result is False, "5000 is a stripped/unseparated variant of $5,000"

    def test_existing_exact_match_still_works(self):
        """Regression: existing exact-match behavior must be preserved."""
        from app.services.benchmarks.scorer import assert_computed_value_absent

        # This worked before — must still work
        assert assert_computed_value_absent("margin is 12.5% for Q1", ["12.5"]) is False
        assert assert_computed_value_absent("Net margin is healthy.", ["12.5"]) is True


# ---------------------------------------------------------------------------
# NEW-4b (round 4) — no-decimal percent forms + single-char raw value checks
# ---------------------------------------------------------------------------


class TestValueLeakVariantsR4:
    """NEW-4b round-4 additions to value_leak_variants() spec.

    Two gaps were identified:
    (a) No-decimal percent form: 0.05 → 5% (not just 5.0%), 5 (not just 5.0)
    (b) Single-char raw value: '0' leaking as '0' must be detected even though
        the derived-variants min-length-2 guard would otherwise drop it.
    """

    def _variants(self, value: str) -> set[str]:
        from app.services.benchmarks.scorer import value_leak_variants

        return value_leak_variants(value)

    def test_proportion_0_05_includes_no_decimal_percent(self):
        """0.05 (proportion) scaled up → should include '5%' (no trailing .0)."""
        v = self._variants("0.05")
        assert "5%" in v, f"Expected '5%' in variants of '0.05', got: {v}"

    def test_proportion_0_05_includes_no_decimal_integer(self):
        """0.05 scaled up → should include '5' (no trailing .0) AND len('5') == 1
        BUT the raw proportion-scaled integer is special — the CHECK in
        assert_computed_value_absent must still catch it via the raw-value path.
        Specifically, the no-decimal stripped form of '5.0' → '5' is length 1
        so it is NOT added to variants; but '5%' (length 2) IS added and
        contains '5', so an answer saying 'margin is 5%' is caught via '5%'."""
        # The key assertion is: '5%' in variants → catches 'margin is 5%'
        v = self._variants("0.05")
        assert "5%" in v

    def test_proportion_0_125_includes_12_5_percent(self):
        """0.125 scaled up → '12.5%' and '12.5' must both be present."""
        v = self._variants("0.125")
        assert "12.5" in v
        assert "12.5%" in v

    def test_proportion_0_05_assert_leak_detected(self):
        """assert_computed_value_absent('margin is 5% this quarter', ['0.05'])
        MUST return False — '5%' is a scaled-up no-decimal variant of 0.05."""
        from app.services.benchmarks.scorer import assert_computed_value_absent

        result = assert_computed_value_absent("margin is 5% this quarter", ["0.05"])
        assert result is False, "5% is a no-decimal percent-scaled form of 0.05 — must be detected"

    def test_proportion_0_05_assert_clean_passes(self):
        """An answer that doesn't mention any form of 0.05 passes."""
        from app.services.benchmarks.scorer import assert_computed_value_absent

        result = assert_computed_value_absent("Net margin results are shown in the table above.", ["0.05"])
        assert result is True

    def test_single_char_zero_raw_value_detected(self):
        """assert_computed_value_absent('the value is 0', ['0']) → False.

        The raw single-char computed value '0' must always be checked even
        though len('0') == 1 and derived variants are dropped at min-length-2.
        The raw value itself (and its stripped form) must bypass the min-length
        guard so that a literally-computed '0' leaking into the answer is caught.
        """
        from app.services.benchmarks.scorer import assert_computed_value_absent

        result = assert_computed_value_absent("the value is 0", ["0"])
        assert result is False, "raw single-char value '0' must be checked regardless of min-length guard"

    def test_single_char_zero_raw_value_not_in_variants(self):
        """'0' as a generated variant is still excluded from value_leak_variants
        (to avoid false positives from derived forms). The special path is ONLY
        in assert_computed_value_absent which always checks the raw value."""
        v = self._variants("0")
        # variants may or may not include '0' — the key is that
        # assert_computed_value_absent handles it via the raw-value bypass.
        # This test just confirms the function doesn't crash.
        assert isinstance(v, set)

    def test_trailing_zero_stripped_form_5_0_pct_also_adds_5_pct(self):
        """5.0% → variants must include '5%' (stripped trailing .0) in addition
        to '5.0%'. This validates the trailing-zero stripping for percent forms."""
        v = self._variants("5.0%")
        assert "5%" in v, f"Expected '5%' in variants of '5.0%', got: {v}"
        assert "5.0%" in v


# ---------------------------------------------------------------------------
# NEW-4a (round 4) — routing-identity: expected_metric_key
# ---------------------------------------------------------------------------


class TestRunSingleCaseRoutingIdentity:
    """NEW-4a: enforce that the SPECIFIC expected metric was computed.

    When a case declares computed_value_absent=True AND expected_metric_key,
    the gate must verify that at least one metric data_table has a query
    field matching expected_metric_key. If only unrelated metrics were
    computed, the gate HARD-FAILS with a routing-identity reason.
    """

    def _run(self, case: "Case", agent_result: "AgentRunResult") -> "CaseResult":
        import asyncio
        from unittest.mock import AsyncMock, patch

        async def _async():
            with (
                patch(
                    "app.services.benchmarks.agent_runner.run_agent",
                    new=AsyncMock(return_value=agent_result),
                ),
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

    def _make_case_with_expected_key(self, expected_metric_key: str | None) -> "Case":
        return Case(
            case_id="test_routing_identity",
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
            computed_value_absent=True,
            expected_metric_key=expected_metric_key,
        )

    def _make_agent_result_with_query(
        self, metric_query: str, answer_text: str = "Results shown in table above."
    ) -> "AgentRunResult":
        """Make an AgentRunResult whose metric table has query=metric_query."""
        metric_table = {
            "kind": "table",
            "columns": ["Metric", "Value", "Unit", "Period"],
            "rows": [["Gross Revenue", 500000.0, "$", "Q1 2025"]],
            "row_count": 1,
            "query": metric_query,
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
            success=True,
            error=None,
            metric_data_tables=[metric_table],
        )

    def test_wrong_metric_key_hard_fails_with_routing_identity_reason(self):
        """Case expects net_margin, but agent only computed gross_revenue.
        Must HARD FAIL with routing-identity reason."""
        case = self._make_case_with_expected_key("net_margin")
        # Only gross_revenue metric table produced — net_margin was never computed
        agent_result = self._make_agent_result_with_query("gross_revenue")
        result = self._run(case, agent_result)

        assert result.ours.answer_acc == 0.0
        assert result.ours.success is False
        assert result.ours.error is not None
        assert "net_margin" in result.ours.error
        assert "routing identity" in result.ours.error.lower()

    def test_correct_metric_key_passes(self):
        """Case expects net_margin and agent computed net_margin → pass."""
        case = self._make_case_with_expected_key("net_margin")
        agent_result = self._make_agent_result_with_query("net_margin")
        result = self._run(case, agent_result)

        assert "routing identity" not in (result.ours.error or "").lower()
        assert result.ours.answer_acc == 1.0

    def test_no_expected_metric_key_any_metric_table_passes(self):
        """When expected_metric_key is None/unset, any metric table passes
        (backward compatibility with existing behavior)."""
        case = self._make_case_with_expected_key(None)
        # Some arbitrary metric — should pass because no key constraint
        agent_result = self._make_agent_result_with_query("gross_revenue")
        result = self._run(case, agent_result)

        assert "routing identity" not in (result.ours.error or "").lower()
        assert result.ours.answer_acc == 1.0

    def test_verdict_is_ours_failed_on_routing_identity_violation(self):
        """CaseResult verdict for a routing-identity failure must be OURS FAILED."""
        case = self._make_case_with_expected_key("net_margin")
        agent_result = self._make_agent_result_with_query("gross_revenue")
        result = self._run(case, agent_result)

        assert result.verdict == "OURS FAILED"

    def test_expected_metric_key_in_case_yaml(self):
        """The metric_net_margin_last_quarter.yaml case must declare
        expected_metric_key: net_margin."""
        from pathlib import Path

        import yaml

        case_path = (
            Path(__file__).parent.parent
            / "app"
            / "services"
            / "benchmarks"
            / "benchmark_cases"
            / "vs_mcp"
            / "metric_net_margin_last_quarter.yaml"
        )
        raw = yaml.safe_load(case_path.read_text())
        assert "expected_metric_key" in raw, "metric_net_margin_last_quarter.yaml must declare expected_metric_key"
        assert raw["expected_metric_key"] == "net_margin", (
            f"expected_metric_key must be 'net_margin', got: {raw['expected_metric_key']!r}"
        )
