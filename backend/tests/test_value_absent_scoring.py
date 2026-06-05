"""TDD test for NEW-4 Part B: value-absent scoring helper.

The anti-hallucination invariant for metric cases: the COMPUTED VALUE
must NOT appear verbatim in the model-visible answer text. Numbers must
come from the data_table SSE event, not the LLM's prose.

Grill NEW-4: adds `assert_computed_value_absent` to scorer.py to enforce
that computed metric values do NOT leak into the model's text answer.

Usage from the benchmark runner:
  For cases with `computed_value_absent: true` in the YAML, the runner
  extracts numeric strings from the data_table tool result and calls
  assert_computed_value_absent(answer_text, extracted_values). If any
  value leaks, the case score is capped at 0.0 (hard fail — this is
  the primary anti-hallucination invariant for metric cases).
"""

from __future__ import annotations

import pytest

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
