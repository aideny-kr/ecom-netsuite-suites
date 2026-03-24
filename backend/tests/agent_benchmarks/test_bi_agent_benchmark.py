"""BI agent benchmark tests."""

from pathlib import Path

from tests.agent_benchmarks.benchmark_runner import BenchmarkRunner

CASES_DIR = Path(__file__).resolve().parent / "benchmark_cases"


class TestBiAgentBenchmark:

    def test_bi_benchmark_cases_load(self):
        runner = BenchmarkRunner()
        cases = runner.load_cases("bi_agent", CASES_DIR)
        assert len(cases) >= 4
        for case in cases:
            assert case.agent_id == "bi-agent"
            assert case.query

    def test_bi_evaluate_revenue_case(self):
        runner = BenchmarkRunner()
        cases = runner.load_cases("bi_agent", CASES_DIR)
        revenue_case = next(c for c in cases if "revenue" in c.query.lower())

        # Mock a successful response
        mock_result = type("R", (), {
            "data": "Revenue by region: US $1.2M, EU $800K this quarter.",
            "tool_calls_log": [{"tool": "bigquery_sql"}],
        })()

        score = runner.evaluate(mock_result, revenue_case, cost=0.15, latency_ms=5000)
        assert score.accuracy > 0  # Has "revenue", "region", "quarter"
        assert score.tool_accuracy == 1.0  # bigquery_sql present
        assert score.cost_ok is True
        assert score.latency_ok is True

    def test_bi_evaluate_cohort_case_partial_tools(self):
        runner = BenchmarkRunner()
        cases = runner.load_cases("bi_agent", CASES_DIR)
        cohort_case = next(c for c in cases if "cohort" in c.query.lower())

        # Mock a response that only uses bigquery_sql (missing pivot)
        mock_result = type("R", (), {
            "data": "Year-over-year retention by cohort shows improvement.",
            "tool_calls_log": [{"tool": "bigquery_sql"}],
        })()

        score = runner.evaluate(mock_result, cohort_case, cost=0.30, latency_ms=10000)
        assert score.tool_accuracy == 0.5  # Only 1 of 2 expected tools
        assert score.accuracy > 0

    def test_bi_evaluate_cost_over_budget(self):
        runner = BenchmarkRunner()
        cases = runner.load_cases("bi_agent", CASES_DIR)
        top_case = next(c for c in cases if "top 10" in c.query.lower())

        mock_result = type("R", (), {
            "data": "Top 10 customers by lifetime value listed.",
            "tool_calls_log": [{"tool": "bigquery_sql"}],
        })()

        score = runner.evaluate(mock_result, top_case, cost=0.50, latency_ms=5000)
        assert score.cost_ok is False  # max_cost is 0.20

    def test_bi_pass_at_5(self):
        runner = BenchmarkRunner()
        from tests.agent_benchmarks.benchmark_runner import BenchmarkScore

        scores = [
            BenchmarkScore(accuracy=0.9, tool_accuracy=1.0, cost=0.10, latency_ms=5000, cost_ok=True, latency_ok=True),
            BenchmarkScore(accuracy=0.6, tool_accuracy=1.0, cost=0.10, latency_ms=5000, cost_ok=True, latency_ok=True),
            BenchmarkScore(accuracy=0.85, tool_accuracy=1.0, cost=0.10, latency_ms=5000, cost_ok=True, latency_ok=True),
            BenchmarkScore(accuracy=0.95, tool_accuracy=1.0, cost=0.10, latency_ms=5000, cost_ok=True, latency_ok=True),
            BenchmarkScore(accuracy=0.80, tool_accuracy=1.0, cost=0.10, latency_ms=5000, cost_ok=True, latency_ok=True),
        ]
        pass_rate = runner.compute_pass_at_k(scores, expected_accuracy=0.8, k=5)
        assert pass_rate == 0.8  # 4 of 5 pass (0.6 fails)

    def test_unified_baseline_cases_exist(self):
        runner = BenchmarkRunner()
        cases = runner.load_cases("unified_agent", CASES_DIR)
        # Should include the BI-related cases mirrored for baseline
        bi_queries = [
            c.query
            for c in cases
            if "revenue" in c.query.lower() or "retention" in c.query.lower()
        ]
        assert len(bi_queries) >= 1

    def test_bi_vs_unified_report(self):
        runner = BenchmarkRunner()
        from tests.agent_benchmarks.benchmark_runner import BenchmarkScore

        agent_scores = [
            BenchmarkScore(accuracy=0.9, tool_accuracy=1.0, cost=0.15, latency_ms=5000, cost_ok=True, latency_ok=True),
            BenchmarkScore(accuracy=0.85, tool_accuracy=1.0, cost=0.20, latency_ms=6000, cost_ok=True, latency_ok=True),
        ]
        baseline_scores = [
            BenchmarkScore(accuracy=0.7, tool_accuracy=0.5, cost=0.25, latency_ms=8000, cost_ok=True, latency_ok=True),
            BenchmarkScore(accuracy=0.65, tool_accuracy=0.5, cost=0.30, latency_ms=9000, cost_ok=True, latency_ok=True),
        ]

        report = runner.build_report(
            agent_id="bi-agent",
            baseline_id="unified-agent",
            agent_scores=agent_scores,
            baseline_scores=baseline_scores,
        )
        assert report.agent_id == "bi-agent"
        assert report.baseline_id == "unified-agent"
        assert report.agent_accuracy > report.baseline_accuracy
        assert report.improvement_pct > 0
