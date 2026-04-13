"""Tests for BenchmarkRunner — evaluation/scoring engine for agent benchmarks.

TDD Phase 1: All tests written BEFORE implementation.
Only depends on AgentResult from base_agent.py — no routing, no registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.services.chat.agents.base_agent import AgentResult
from app.services.benchmarks.benchmark_runner import (
    BenchmarkCase,
    BenchmarkReport,
    BenchmarkRunner,
    BenchmarkScore,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> BenchmarkRunner:
    return BenchmarkRunner()


def _make_result(
    data: str = "",
    tool_calls_log: list[dict] | None = None,
    success: bool = True,
) -> AgentResult:
    """Factory for AgentResult with sensible defaults."""
    return AgentResult(
        success=success,
        data=data,
        tool_calls_log=tool_calls_log or [],
        agent_name="test-agent",
    )


def _write_cases_yaml(tmp_path: Path, agent_id: str, cases: list[dict]) -> Path:
    """Write benchmark case YAML files into a temp dir structure."""
    agent_dir = tmp_path / agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    for i, case in enumerate(cases):
        (agent_dir / f"case_{i}.yaml").write_text(yaml.dump(case))
    return tmp_path


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class TestLoadBenchmarkCases:
    def test_load_benchmark_cases_from_yaml(self, runner: BenchmarkRunner, tmp_path: Path):
        cases_data = [
            {"query": "Q1", "expected_answer_contains": ["a"]},
            {"query": "Q2", "expected_answer_contains": ["b"]},
            {"query": "Q3", "expected_answer_contains": ["c"]},
        ]
        base_dir = _write_cases_yaml(tmp_path, "test-agent", cases_data)
        cases = runner.load_cases("test-agent", base_dir)
        assert len(cases) == 3
        assert all(isinstance(c, BenchmarkCase) for c in cases)

    def test_benchmark_case_has_required_fields(self, tmp_path: Path):
        data = {
            "query": "What is the margin?",
            "expected_tools": ["netsuite_suiteql"],
            "expected_answer_contains": ["margin", "percent"],
            "max_cost": 0.10,
            "max_latency_ms": 10000,
        }
        (tmp_path / "case.yaml").write_text(yaml.dump(data))
        case = BenchmarkCase(**yaml.safe_load((tmp_path / "case.yaml").read_text()))
        assert case.query == "What is the margin?"
        assert case.expected_tools == ["netsuite_suiteql"]
        assert case.expected_answer_contains == ["margin", "percent"]
        assert case.max_cost == 0.10
        assert case.max_latency_ms == 10000

    def test_benchmark_case_optional_fields_default(self):
        case = BenchmarkCase(
            query="Simple question",
            expected_answer_contains=["answer"],
        )
        assert case.expected_accuracy == 0.8
        assert case.max_cost == 0.50
        assert case.max_latency_ms == 15000
        assert case.expected_tools == []
        assert case.tags == []
        assert case.notes == ""


# ---------------------------------------------------------------------------
# Accuracy evaluation
# ---------------------------------------------------------------------------


class TestEvaluateAccuracy:
    def test_evaluate_correct_answer(self, runner: BenchmarkRunner):
        result = _make_result(data="The margin is 45 percent on SKU-1234")
        case = BenchmarkCase(
            query="What is the margin on SKU-1234?",
            expected_answer_contains=["margin", "percent"],
        )
        score = runner.evaluate(result, case, cost=0.05, latency_ms=3000)
        assert score.accuracy == 1.0

    def test_evaluate_partial_keywords(self, runner: BenchmarkRunner):
        """AgentResult data missing 2 of 4 expected keywords -> score.accuracy == 0.5"""
        result = _make_result(data="The margin is 45% on this item")
        case = BenchmarkCase(
            query="Test",
            expected_answer_contains=["margin", "45%", "SKU-1234", "widget"],
        )
        score = runner.evaluate(result, case, cost=0.05, latency_ms=3000)
        # "margin" ✓, "45%" ✓, "SKU-1234" ✗, "widget" ✗ → 2/4 = 0.5
        assert score.accuracy == 0.5

    def test_evaluate_case_insensitive(self, runner: BenchmarkRunner):
        result = _make_result(data="MARGIN is high")
        case = BenchmarkCase(
            query="Test",
            expected_answer_contains=["margin"],
        )
        score = runner.evaluate(result, case, cost=0.05, latency_ms=3000)
        assert score.accuracy == 1.0


# ---------------------------------------------------------------------------
# Tool usage evaluation
# ---------------------------------------------------------------------------


class TestEvaluateToolUsage:
    def test_evaluate_tool_usage_correct(self, runner: BenchmarkRunner):
        result = _make_result(
            data="result",
            tool_calls_log=[{"tool": "netsuite_suiteql"}],
        )
        case = BenchmarkCase(
            query="Test",
            expected_answer_contains=["result"],
            expected_tools=["netsuite_suiteql"],
        )
        score = runner.evaluate(result, case, cost=0.05, latency_ms=3000)
        assert score.tool_accuracy == 1.0

    def test_evaluate_tool_usage_extra_tools_ok(self, runner: BenchmarkRunner):
        """Agent used 3 tools, only 2 expected — superset is fine."""
        result = _make_result(
            data="result",
            tool_calls_log=[
                {"tool": "netsuite_suiteql"},
                {"tool": "rag_search"},
                {"tool": "netsuite_get_record"},
            ],
        )
        case = BenchmarkCase(
            query="Test",
            expected_answer_contains=["result"],
            expected_tools=["netsuite_suiteql", "rag_search"],
        )
        score = runner.evaluate(result, case, cost=0.05, latency_ms=3000)
        assert score.tool_accuracy == 1.0

    def test_evaluate_tool_usage_missing_tools(self, runner: BenchmarkRunner):
        """Agent used 1 tool, 2 expected — missing 1 → 0.5."""
        result = _make_result(
            data="result",
            tool_calls_log=[{"tool": "netsuite_suiteql"}],
        )
        case = BenchmarkCase(
            query="Test",
            expected_answer_contains=["result"],
            expected_tools=["netsuite_suiteql", "pivot_query_result"],
        )
        score = runner.evaluate(result, case, cost=0.05, latency_ms=3000)
        assert score.tool_accuracy == 0.5


# ---------------------------------------------------------------------------
# Cost and latency evaluation
# ---------------------------------------------------------------------------


class TestEvaluateCostLatency:
    def test_evaluate_cost_within_budget(self, runner: BenchmarkRunner):
        result = _make_result(data="ok")
        case = BenchmarkCase(
            query="Test",
            expected_answer_contains=["ok"],
            max_cost=0.10,
        )
        score = runner.evaluate(result, case, cost=0.08, latency_ms=5000)
        assert score.cost_ok is True

    def test_evaluate_cost_over_budget(self, runner: BenchmarkRunner):
        result = _make_result(data="ok")
        case = BenchmarkCase(
            query="Test",
            expected_answer_contains=["ok"],
            max_cost=0.10,
        )
        score = runner.evaluate(result, case, cost=0.15, latency_ms=5000)
        assert score.cost_ok is False

    def test_evaluate_latency_within_limit(self, runner: BenchmarkRunner):
        result = _make_result(data="ok")
        case = BenchmarkCase(
            query="Test",
            expected_answer_contains=["ok"],
            max_latency_ms=15000,
        )
        score = runner.evaluate(result, case, cost=0.05, latency_ms=5000)
        assert score.latency_ok is True

    def test_evaluate_latency_over_limit(self, runner: BenchmarkRunner):
        result = _make_result(data="ok")
        case = BenchmarkCase(
            query="Test",
            expected_answer_contains=["ok"],
            max_latency_ms=15000,
        )
        score = runner.evaluate(result, case, cost=0.05, latency_ms=20000)
        assert score.latency_ok is False


# ---------------------------------------------------------------------------
# pass@k
# ---------------------------------------------------------------------------


class TestPassAtK:
    def _make_score(self, accuracy: float) -> BenchmarkScore:
        return BenchmarkScore(
            accuracy=accuracy,
            tool_accuracy=1.0,
            cost=0.05,
            latency_ms=3000,
            cost_ok=True,
            latency_ok=True,
        )

    def test_pass_at_k_all_pass(self, runner: BenchmarkRunner):
        scores = [self._make_score(0.9) for _ in range(5)]
        assert runner.compute_pass_at_k(scores, expected_accuracy=0.8, k=5) == 1.0

    def test_pass_at_k_partial(self, runner: BenchmarkRunner):
        scores = [
            self._make_score(0.9),
            self._make_score(0.9),
            self._make_score(0.9),
            self._make_score(0.5),
            self._make_score(0.5),
        ]
        result = runner.compute_pass_at_k(scores, expected_accuracy=0.8, k=5)
        assert result == pytest.approx(0.6)

    def test_pass_at_k_none_pass(self, runner: BenchmarkRunner):
        scores = [self._make_score(0.3) for _ in range(5)]
        assert runner.compute_pass_at_k(scores, expected_accuracy=0.8, k=5) == 0.0


# ---------------------------------------------------------------------------
# Comparison report
# ---------------------------------------------------------------------------


class TestComparisonReport:
    def _make_score(self, accuracy: float, cost: float = 0.05, latency_ms: int = 3000) -> BenchmarkScore:
        return BenchmarkScore(
            accuracy=accuracy,
            tool_accuracy=1.0,
            cost=cost,
            latency_ms=latency_ms,
            cost_ok=True,
            latency_ok=True,
        )

    def test_comparison_report_fields(self, runner: BenchmarkRunner):
        agent_scores = [self._make_score(0.9, cost=0.08, latency_ms=4000) for _ in range(5)]
        baseline_scores = [self._make_score(0.7, cost=0.12, latency_ms=8000) for _ in range(5)]
        report = runner.build_report(
            agent_id="pricing-agent",
            baseline_id="unified-agent",
            agent_scores=agent_scores,
            baseline_scores=baseline_scores,
            expected_accuracy=0.8,
        )
        assert isinstance(report, BenchmarkReport)
        assert report.agent_id == "pricing-agent"
        assert report.baseline_id == "unified-agent"
        assert hasattr(report, "agent_accuracy")
        assert hasattr(report, "baseline_accuracy")
        assert hasattr(report, "agent_cost")
        assert hasattr(report, "baseline_cost")
        assert hasattr(report, "improvement_pct")

    def test_comparison_report_improvement_positive(self, runner: BenchmarkRunner):
        agent_scores = [self._make_score(0.9) for _ in range(5)]
        baseline_scores = [self._make_score(0.7) for _ in range(5)]
        report = runner.build_report(
            agent_id="pricing-agent",
            baseline_id="unified-agent",
            agent_scores=agent_scores,
            baseline_scores=baseline_scores,
            expected_accuracy=0.8,
        )
        assert report.improvement_pct == pytest.approx(28.57, abs=0.1)

    def test_comparison_report_improvement_negative(self, runner: BenchmarkRunner):
        agent_scores = [self._make_score(0.6) for _ in range(5)]
        baseline_scores = [self._make_score(0.8) for _ in range(5)]
        report = runner.build_report(
            agent_id="pricing-agent",
            baseline_id="unified-agent",
            agent_scores=agent_scores,
            baseline_scores=baseline_scores,
            expected_accuracy=0.8,
        )
        assert report.improvement_pct == pytest.approx(-25.0, abs=0.1)
