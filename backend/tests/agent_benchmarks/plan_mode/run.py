"""Plan Mode behavioral eval — 50-query golden suite + CI gate.

Runs the `is_financial_ambiguous` regex against 50 hand-authored CFO/ops queries
and asserts that the contract holds:

  - financial_ambiguous (10) MUST trigger clarify (regex returns True).
  - financial_clear     (10) MUST NOT trigger clarify (regex returns False).
  - non_financial_clear (20) MUST NOT trigger clarify (regex returns False).
  - non_financial_ambiguous (10) is warn-only — false positives are tracked
    but do not fail the run (regex is intentionally conservative on these).

Aggregate ask-rate (clarify_triggers / 50) must land in [10%, 30%]. The target
is 20% (10 financial_ambiguous fire, nothing else). The upper bound gives a bit
of slack for any non_financial_ambiguous false positives that may slip in as
the regex evolves; >30% means the regex has gotten too aggressive.

Exit codes:
  0 — all must_pass cases pass and ask-rate within bounds.
  1 — at least one must_pass case failed OR ask-rate outside [10%, 30%].

Runtime: <100ms (no LLM calls). Safe for CI on every PR.

Why static / regex-only?
  The behavior under test IS the regex (`is_financial_ambiguous`). It's the
  contract that gates Plan Mode. Running the full unified agent here would
  cost real LLM dollars per PR and add noise (model-side variance). The
  full E2E pass — dispatching the unified agent, inspecting `tool_calls_log[0]`
  — can be added as a NIGHTLY job later if the regex contract proves
  insufficient.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from app.services.chat.plan_mode.ambiguity_signal import is_financial_ambiguous

_CASES_FILE = Path(__file__).parent / "cases.yaml"

# Aggregate ask-rate bounds. Target = 20% (10 financial_ambiguous out of 50).
# Lower bound catches a regex regression that drops ambiguity coverage.
# Upper bound catches a regex that's gotten too aggressive (false-positive ops queries).
_ASK_RATE_MIN = 0.10
_ASK_RATE_MAX = 0.30


def _evaluate_case(case: dict) -> tuple[bool, str | None]:
    """Run the regex against one case. Return (passed, failure_reason_or_None).

    The case dict has:
      query: str
      expected_first_tool: "clarify" (regex must return True), OR
      expected_first_tool_not: "clarify" (regex must return False)
      must_pass: bool
    """
    query = case["query"]
    triggered = is_financial_ambiguous(query)

    if "expected_first_tool" in case:
        # Bucket asserts the regex SHOULD trigger.
        if case["expected_first_tool"] != "clarify":
            return (False, f"unsupported expected_first_tool={case['expected_first_tool']!r}")
        if not triggered:
            return (False, f"expected clarify but regex returned False: {query!r}")
        return (True, None)

    if "expected_first_tool_not" in case:
        # Bucket asserts the regex should NOT trigger.
        if case["expected_first_tool_not"] != "clarify":
            return (False, f"unsupported expected_first_tool_not={case['expected_first_tool_not']!r}")
        if triggered:
            return (False, f"unexpected clarify trigger: {query!r}")
        return (True, None)

    return (False, f"case missing expected_first_tool / expected_first_tool_not: {query!r}")


def main() -> int:
    cases_by_bucket: dict[str, list[dict]] = yaml.safe_load(_CASES_FILE.read_text())

    failures: list[str] = []
    warnings: list[str] = []
    triggers_total = 0
    cases_total = 0
    bucket_counts: dict[str, dict[str, int]] = {}

    for bucket, cases in cases_by_bucket.items():
        bucket_counts[bucket] = {"total": 0, "triggered": 0, "passed": 0, "failed": 0}
        for case in cases:
            cases_total += 1
            bucket_counts[bucket]["total"] += 1

            triggered = is_financial_ambiguous(case["query"])
            if triggered:
                triggers_total += 1
                bucket_counts[bucket]["triggered"] += 1

            passed, reason = _evaluate_case(case)
            if passed:
                bucket_counts[bucket]["passed"] += 1
            else:
                bucket_counts[bucket]["failed"] += 1
                msg = f"[{bucket}] {reason}"
                if case.get("must_pass", False):
                    failures.append(msg)
                else:
                    warnings.append(msg)

    # Sanity check: must total exactly 50 cases.
    if cases_total != 50:
        print(f"FAIL: expected 50 cases, found {cases_total}")
        return 1

    ask_rate = triggers_total / cases_total

    # Print summary.
    print(f"Plan Mode behavioral eval — {cases_total} cases")
    print(f"Ask-rate: {triggers_total}/{cases_total} = {ask_rate:.2%}")
    print()
    print("Per-bucket:")
    for bucket, counts in bucket_counts.items():
        print(
            f"  {bucket:>26s}: total={counts['total']:>2}  "
            f"clarify_triggered={counts['triggered']:>2}  "
            f"passed={counts['passed']:>2}  failed={counts['failed']:>2}"
        )

    if warnings:
        print()
        print(f"Warnings ({len(warnings)}, non-fatal):")
        for w in warnings:
            print(f"  - {w}")

    if failures:
        print()
        print(f"FAIL: {len(failures)} must_pass case(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1

    if not (_ASK_RATE_MIN <= ask_rate <= _ASK_RATE_MAX):
        print()
        print(f"FAIL: ask-rate {ask_rate:.2%} outside [{_ASK_RATE_MIN:.0%}, {_ASK_RATE_MAX:.0%}]")
        return 1

    print()
    print(f"PASS: {cases_total} cases, ask-rate {ask_rate:.2%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
