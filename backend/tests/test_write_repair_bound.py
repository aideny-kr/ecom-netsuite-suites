"""Tests for write_repair_bound.py — the persisted bound on the post-approval
write-repair loop (requirement D of the agentic-repair design).

Pure, DB-free: `extract_netsuite_error_details` / `compute_failure_fingerprint`
/ `decide_repair_bound` are the mechanism the orchestrator's approve-failure
path calls; this file proves the decision table in isolation before any
orchestrator wiring exists. See `.superpowers/sdd/2026-08-19-agentic-netsuite-write-loop/agentic-repair-design.json`
`bounding` for the ruling this pins:

  - stall: this rejection's fingerprint equals the PREVIOUS failure's in the
    chain (recomposing changed nothing NetSuite cares about) — fires even at
    attempt 0 (the root's own repeat, once re-approved by a human, would
    still be a stall against itself... but in practice attempt 0 has no
    predecessor, so stall can only fire from attempt 1 onward).
  - budget: repair_attempt >= max_attempts (ceiling matches
    WriteRepairState.max_attempts=2).
  - error: the rejection could not be fingerprinted at all (no o:errorDetails
    AND an empty raw fallback) — never guess a repair is safe from nothing.
  - reenter: otherwise — attempt consumes budget regardless of whether a
    later fingerprint comparison would have found a stall.
"""

from __future__ import annotations

import json

from app.services.chat.write_repair_bound import (
    RepairBoundDecision,
    compute_failure_fingerprint,
    decide_repair_bound,
    extract_netsuite_error_details,
)

# ---------------------------------------------------------------------------
# extract_netsuite_error_details — mechanical o:errorDetails[].detail
# traversal, never regex/field-name parsing.
# ---------------------------------------------------------------------------


class TestExtractNetsuiteErrorDetails:
    def test_extracts_detail_strings_from_top_level_errordetails(self):
        raw = json.dumps(
            {
                "type": "https://api.netsuite.com/problem",
                "o:errorDetails": [
                    {"detail": "Please enter value(s) for: Primary Subsidiary.", "o:errorCode": "MANDATORY_FIELD"},
                    {"detail": "Please enter value(s) for: Currency.", "o:errorCode": "MANDATORY_FIELD"},
                ],
            }
        )
        details = extract_netsuite_error_details(raw)
        assert details == [
            "Please enter value(s) for: Primary Subsidiary.",
            "Please enter value(s) for: Currency.",
        ]

    def test_extracts_from_nested_wrapper(self):
        """NetSuite responses may wrap the problem doc — mechanical traversal
        must find o:errorDetails regardless of nesting depth."""
        raw = json.dumps({"error": {"body": {"o:errorDetails": [{"detail": "Invalid field value."}]}}})
        assert extract_netsuite_error_details(raw) == ["Invalid field value."]

    def test_ignores_non_string_or_blank_detail_entries(self):
        raw = json.dumps(
            {
                "o:errorDetails": [
                    {"detail": "Real detail."},
                    {"detail": ""},
                    {"detail": None},
                    {"no_detail_key": True},
                    "not a dict",
                ]
            }
        )
        assert extract_netsuite_error_details(raw) == ["Real detail."]

    def test_no_errordetails_key_returns_empty(self):
        raw = json.dumps({"error": "HTTP 400: Please enter value(s) for: Primary Subsidiary."})
        assert extract_netsuite_error_details(raw) == []

    def test_unparseable_json_returns_empty(self):
        assert extract_netsuite_error_details("not json at all {{{") == []

    def test_empty_string_returns_empty(self):
        assert extract_netsuite_error_details("") == []


# ---------------------------------------------------------------------------
# compute_failure_fingerprint
# ---------------------------------------------------------------------------


class TestComputeFailureFingerprint:
    def test_fingerprint_from_errordetails_is_stable_across_key_order(self):
        raw_a = json.dumps({"o:errorDetails": [{"detail": "B."}, {"detail": "A."}]})
        raw_b = json.dumps({"o:errorDetails": [{"detail": "A."}, {"detail": "B."}]})
        fp_a, _ = compute_failure_fingerprint(raw_a, "")
        fp_b, _ = compute_failure_fingerprint(raw_b, "")
        assert fp_a is not None
        assert fp_a == fp_b

    def test_different_details_produce_different_fingerprints(self):
        raw_subsidiary = json.dumps({"o:errorDetails": [{"detail": "Please enter value(s) for: Primary Subsidiary."}]})
        raw_currency = json.dumps({"o:errorDetails": [{"detail": "Please enter value(s) for: Currency."}]})
        fp_1, _ = compute_failure_fingerprint(raw_subsidiary, "")
        fp_2, _ = compute_failure_fingerprint(raw_currency, "")
        assert fp_1 != fp_2

    def test_falls_back_to_raw_text_when_no_errordetails(self):
        raw = json.dumps({"error": "HTTP 400: Please enter value(s) for: Primary Subsidiary."})
        fp, basis = compute_failure_fingerprint(raw, "HTTP 400: Please enter value(s) for: Primary Subsidiary.")
        assert fp is not None
        assert basis == "HTTP 400: Please enter value(s) for: Primary Subsidiary."

    def test_unparseable_and_empty_fallback_yields_no_fingerprint(self):
        """The 'error' exit case: nothing to fingerprint at all."""
        fp, basis = compute_failure_fingerprint("not json {{{", "   ")
        assert fp is None
        assert basis == ""

    def test_raw_fallback_capped(self):
        long_text = "x" * 5000
        fp, basis = compute_failure_fingerprint(json.dumps({"error": long_text}), long_text)
        assert fp is not None
        assert len(basis) <= 2000


# ---------------------------------------------------------------------------
# decide_repair_bound — pure decision table
# ---------------------------------------------------------------------------


class TestDecideRepairBound:
    def test_root_first_failure_reenters(self):
        decision = decide_repair_bound(
            current_attempt=0, current_fingerprint="fp1", previous_fingerprint=None, max_attempts=2
        )
        assert decision == RepairBoundDecision(reason="reenter", next_attempt=1)

    def test_no_fingerprint_is_error_regardless_of_attempt_or_budget(self):
        decision = decide_repair_bound(
            current_attempt=0, current_fingerprint=None, previous_fingerprint=None, max_attempts=2
        )
        assert decision.reason == "error"

    def test_identical_fingerprint_to_previous_is_stall_even_with_budget_left(self):
        decision = decide_repair_bound(
            current_attempt=1, current_fingerprint="same", previous_fingerprint="same", max_attempts=2
        )
        assert decision.reason == "stall"

    def test_distinct_fingerprint_under_budget_reenters(self):
        decision = decide_repair_bound(
            current_attempt=1, current_fingerprint="new", previous_fingerprint="old", max_attempts=2
        )
        assert decision == RepairBoundDecision(reason="reenter", next_attempt=2)

    def test_attempt_at_ceiling_exits_budget(self):
        decision = decide_repair_bound(
            current_attempt=2, current_fingerprint="new", previous_fingerprint="old", max_attempts=2
        )
        assert decision.reason == "budget"

    def test_stall_takes_priority_over_budget_when_both_would_fire(self):
        """A stall at the ceiling attempt must still report 'stall', not
        'budget' — the reason names WHY it stopped, not just that it did."""
        decision = decide_repair_bound(
            current_attempt=2, current_fingerprint="same", previous_fingerprint="same", max_attempts=2
        )
        assert decision.reason == "stall"

    def test_genuinely_different_repairs_still_consume_budget_to_the_ceiling(self):
        """Ruling: 'genuinely different repair' still consumes budget
        unconditionally — distinct fingerprints all the way up hit budget,
        not an unbounded re-entry."""
        fp_by_attempt = ["fp0", "fp1", "fp2"]
        decision = None
        previous = None
        for attempt, fp in enumerate(fp_by_attempt):
            decision = decide_repair_bound(
                current_attempt=attempt, current_fingerprint=fp, previous_fingerprint=previous, max_attempts=2
            )
            previous = fp
        assert decision.reason == "budget"
