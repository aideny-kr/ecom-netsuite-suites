"""Integration tests for benchmark runner with real configs."""

from pathlib import Path

import pytest

from tests.agent_benchmarks.benchmark_runner import BenchmarkRunner


CASES_DIR = Path(__file__).resolve().parent / "benchmark_cases"


class TestBenchmarkIntegration:

    def test_load_pricing_cases(self):
        runner = BenchmarkRunner()
        # Directory uses underscore: pricing_agent/
        cases = runner.load_cases("pricing_agent", CASES_DIR)
        assert len(cases) > 0
        for case in cases:
            assert case.query  # Non-empty query
            assert isinstance(case.expected_tools, list)
            assert isinstance(case.expected_answer_contains, list)

    def test_load_unified_cases(self):
        runner = BenchmarkRunner()
        cases = runner.load_cases("unified_agent", CASES_DIR)
        # unified_agent dir should also have benchmark cases
        assert isinstance(cases, list)

    def test_load_nonexistent_agent_returns_empty(self):
        runner = BenchmarkRunner()
        cases = runner.load_cases("nonexistent-agent", CASES_DIR)
        assert cases == []

    def test_pricing_cases_have_expected_tools(self):
        runner = BenchmarkRunner()
        cases = runner.load_cases("pricing_agent", CASES_DIR)
        # At least one case should expect netsuite_suiteql
        tool_sets = [set(c.expected_tools) for c in cases]
        all_tools = set().union(*tool_sets) if tool_sets else set()
        assert "netsuite_suiteql" in all_tools

    def test_pricing_cases_cost_budgets_are_reasonable(self):
        runner = BenchmarkRunner()
        cases = runner.load_cases("pricing_agent", CASES_DIR)
        for case in cases:
            assert case.max_cost <= 1.0, f"Pricing case cost too high: {case.max_cost}"
            assert case.max_latency_ms <= 30000, f"Latency too high: {case.max_latency_ms}"

    def test_evaluate_perfect_result(self):
        runner = BenchmarkRunner()
        cases = runner.load_cases("pricing_agent", CASES_DIR)
        assert len(cases) > 0

        case = cases[0]
        # Build a mock result that hits all expected keywords and tools
        mock_result = type(
            "MockResult",
            (),
            {
                "data": " ".join(case.expected_answer_contains),
                "tool_calls_log": [
                    {"tool": t} for t in case.expected_tools
                ],
            },
        )()

        score = runner.evaluate(mock_result, case, cost=0.01, latency_ms=1000)
        assert score.accuracy == 1.0
        assert score.tool_accuracy == 1.0
        assert score.cost_ok is True
        assert score.latency_ok is True

    def test_pass_at_k_computation(self):
        runner = BenchmarkRunner()
        from tests.agent_benchmarks.benchmark_runner import BenchmarkScore

        scores = [
            BenchmarkScore(
                accuracy=0.9,
                tool_accuracy=1.0,
                cost=0.05,
                latency_ms=3000,
                cost_ok=True,
                latency_ok=True,
            )
            for _ in range(5)
        ]
        pass_rate = runner.compute_pass_at_k(scores, expected_accuracy=0.8, k=5)
        assert pass_rate == 1.0

    def test_pass_at_k_partial(self):
        runner = BenchmarkRunner()
        from tests.agent_benchmarks.benchmark_runner import BenchmarkScore

        scores = [
            BenchmarkScore(
                accuracy=0.9 if i < 3 else 0.5,
                tool_accuracy=1.0,
                cost=0.05,
                latency_ms=3000,
                cost_ok=True,
                latency_ok=True,
            )
            for i in range(5)
        ]
        pass_rate = runner.compute_pass_at_k(scores, expected_accuracy=0.8, k=5)
        assert pass_rate == 3 / 5
