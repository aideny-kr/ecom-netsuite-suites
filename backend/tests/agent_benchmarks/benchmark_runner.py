"""Benchmark evaluation engine for agent performance testing.

Provides scoring, pass@k computation, and comparison reports.
Does NOT implement run_benchmark() — that needs the agent registry (Prompt 3).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

import yaml
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class BenchmarkCase(BaseModel):
    """A single benchmark test case loaded from YAML."""

    query: str
    agent_id: str = ""
    expected_tools: list[str] = Field(default_factory=list)
    expected_answer_contains: list[str] = Field(default_factory=list)
    expected_accuracy: float = 0.8
    max_cost: float = 0.50
    max_latency_ms: int = 15000
    tags: list[str] = Field(default_factory=list)
    notes: str = ""
    # Baseline comparison fields (used by vs-baseline benchmarks)
    bi_agent_advantages: list[str] = Field(default_factory=list)
    baseline_expected_tools: list[str] = Field(default_factory=list)
    baseline_expected_accuracy: float = 0.5


@dataclass
class BenchmarkScore:
    """Score from evaluating a single agent run against a benchmark case."""

    accuracy: float  # keyword hit ratio 0-1
    tool_accuracy: float  # expected tools present ratio 0-1, superset OK
    cost: float
    latency_ms: int
    cost_ok: bool
    latency_ok: bool


@dataclass
class BenchmarkReport:
    """Comparison report between an agent and its baseline."""

    agent_id: str
    baseline_id: str
    agent_accuracy: float
    baseline_accuracy: float
    agent_cost: float
    baseline_cost: float
    agent_latency: float
    baseline_latency: float
    agent_pass_at_5: float
    improvement_pct: float
    cases_run: int


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class BenchmarkRunner:
    """Evaluation/scoring engine for agent benchmarks."""

    def load_cases(self, agent_id: str, base_dir: Path) -> list[BenchmarkCase]:
        """Load all benchmark cases for a given agent from YAML files."""
        agent_dir = base_dir / agent_id
        if not agent_dir.is_dir():
            return []
        cases: list[BenchmarkCase] = []
        for yaml_file in sorted(agent_dir.glob("*.yaml")):
            data = yaml.safe_load(yaml_file.read_text())
            if data is None:
                continue
            cases.append(BenchmarkCase(**data))
        return cases

    def evaluate(
        self,
        result: object,  # AgentResult — duck-typed to avoid circular import
        case: BenchmarkCase,
        cost: float,
        latency_ms: int,
    ) -> BenchmarkScore:
        """Evaluate a single agent result against a benchmark case."""
        # Keyword accuracy (case-insensitive)
        data_lower = str(result.data or "").lower()
        if case.expected_answer_contains:
            hits = sum(
                1
                for kw in case.expected_answer_contains
                if kw.lower() in data_lower
            )
            accuracy = hits / len(case.expected_answer_contains)
        else:
            accuracy = 1.0

        # Tool accuracy (superset OK — agent can use more tools than expected)
        if case.expected_tools:
            used_tools = {
                entry.get("tool", entry.get("name", ""))
                for entry in (result.tool_calls_log or [])
            }
            hits = sum(1 for t in case.expected_tools if t in used_tools)
            tool_accuracy = hits / len(case.expected_tools)
        else:
            tool_accuracy = 1.0

        return BenchmarkScore(
            accuracy=accuracy,
            tool_accuracy=tool_accuracy,
            cost=cost,
            latency_ms=latency_ms,
            cost_ok=cost <= case.max_cost,
            latency_ok=latency_ms <= case.max_latency_ms,
        )

    def compute_pass_at_k(
        self,
        scores: list[BenchmarkScore],
        expected_accuracy: float,
        k: int,
    ) -> float:
        """Compute pass@k: fraction of runs meeting the accuracy threshold."""
        if not scores:
            return 0.0
        # Take the last k scores (or all if fewer)
        relevant = scores[-k:]
        passing = sum(1 for s in relevant if s.accuracy >= expected_accuracy)
        return passing / len(relevant)

    def build_report(
        self,
        agent_id: str,
        baseline_id: str,
        agent_scores: list[BenchmarkScore],
        baseline_scores: list[BenchmarkScore],
        expected_accuracy: float = 0.8,
    ) -> BenchmarkReport:
        """Build a comparison report between agent and baseline scores."""
        agent_accuracy = mean(s.accuracy for s in agent_scores)
        baseline_accuracy = mean(s.accuracy for s in baseline_scores)
        agent_cost = mean(s.cost for s in agent_scores)
        baseline_cost = mean(s.cost for s in baseline_scores)
        agent_latency = mean(s.latency_ms for s in agent_scores)
        baseline_latency = mean(s.latency_ms for s in baseline_scores)

        # Improvement percentage: (agent - baseline) / baseline * 100
        if baseline_accuracy > 0:
            improvement_pct = (
                (agent_accuracy - baseline_accuracy) / baseline_accuracy * 100
            )
        else:
            improvement_pct = 0.0

        return BenchmarkReport(
            agent_id=agent_id,
            baseline_id=baseline_id,
            agent_accuracy=agent_accuracy,
            baseline_accuracy=baseline_accuracy,
            agent_cost=agent_cost,
            baseline_cost=baseline_cost,
            agent_latency=agent_latency,
            baseline_latency=baseline_latency,
            agent_pass_at_5=self.compute_pass_at_k(
                agent_scores, expected_accuracy, k=5
            ),
            improvement_pct=round(improvement_pct, 2),
            cases_run=len(agent_scores),
        )
