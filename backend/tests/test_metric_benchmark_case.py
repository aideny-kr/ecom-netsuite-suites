"""TDD unit test for the named-metric benchmark case.

This test guards the intent of the metric_net_margin_last_quarter benchmark
case: a named-metric ask ("what is our net margin last quarter?") MUST
declare metric_compute as an expected tool, asserting that the agent routes
through the metric catalog rather than falling back to ad-hoc SuiteQL.

Grill R2#19 finding: benchmark CI gate did not cover metric paths, and no
benchmark case existed that exercises metric_compute routing. This test is
the D2 "measured" anchor — the case exists and its declared intent is
machine-verified.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.services.benchmarks.benchmark_runner import BenchmarkCase

CASE_PATH = (
    Path(__file__).parent.parent
    / "app"
    / "services"
    / "benchmarks"
    / "benchmark_cases"
    / "vs_mcp"
    / "metric_net_margin_last_quarter.yaml"
)

REQUIRED_FIELDS = {"query", "expected_tools", "expected_answer_contains"}


class TestMetricNetMarginBenchmarkCase:
    def test_case_file_exists(self):
        """The named-metric benchmark case YAML file must exist."""
        assert CASE_PATH.exists(), (
            f"Missing benchmark case: {CASE_PATH}. Add metric_net_margin_last_quarter.yaml to benchmark_cases/vs_mcp/."
        )

    def test_case_parses_as_benchmark_case(self):
        """The YAML must parse as a valid BenchmarkCase without errors."""
        raw = yaml.safe_load(CASE_PATH.read_text())
        assert raw is not None, "YAML file is empty"
        case = BenchmarkCase(**raw)
        assert case.query.strip(), "query must be non-empty"

    def test_required_fields_present(self):
        """The YAML must contain all required top-level fields."""
        raw = yaml.safe_load(CASE_PATH.read_text())
        missing = REQUIRED_FIELDS - set(raw.keys())
        assert not missing, f"Case YAML missing required fields: {missing}"

    def test_metric_compute_in_expected_tools(self):
        """The case MUST declare metric_compute in expected_tools.

        This is the D2 measured assertion: a named-metric ask ('net margin
        last quarter') should route through metric_compute, not ad-hoc SuiteQL.
        Without this, the benchmark CI gate cannot detect a regression where
        the agent bypasses the metric catalog.
        """
        raw = yaml.safe_load(CASE_PATH.read_text())
        case = BenchmarkCase(**raw)
        assert "metric_compute" in case.expected_tools, (
            f"metric_compute must be in expected_tools for a named-metric ask. Got: {case.expected_tools}"
        )

    def test_expected_answer_contains_nonempty(self):
        """expected_answer_contains must have at least one keyword."""
        raw = yaml.safe_load(CASE_PATH.read_text())
        case = BenchmarkCase(**raw)
        assert len(case.expected_answer_contains) >= 1, "expected_answer_contains must have at least one keyword"

    def test_baseline_fields_present(self):
        """Baseline comparison fields should be declared for vs_mcp cases."""
        raw = yaml.safe_load(CASE_PATH.read_text())
        assert "baseline_expected_tools" in raw, "vs_mcp cases should declare baseline_expected_tools"
        assert "baseline_expected_accuracy" in raw, "vs_mcp cases should declare baseline_expected_accuracy"
