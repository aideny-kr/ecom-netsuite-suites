"""Tests that validate all YAML benchmark case files are well-formed.

Only depends on BenchmarkCase pydantic model — no runtime imports.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import yaml

from tests.agent_benchmarks.benchmark_runner import BenchmarkCase

BENCHMARK_CASES_DIR = Path(__file__).parent / "benchmark_cases"


def _load_all_cases() -> dict[str, list[tuple[str, BenchmarkCase]]]:
    """Load all benchmark cases grouped by agent_id (directory name)."""
    result: dict[str, list[tuple[str, BenchmarkCase]]] = defaultdict(list)
    if not BENCHMARK_CASES_DIR.exists():
        return result
    for agent_dir in sorted(BENCHMARK_CASES_DIR.iterdir()):
        if not agent_dir.is_dir() or agent_dir.name.startswith("_"):
            continue
        for yaml_file in sorted(agent_dir.glob("*.yaml")):
            data = yaml.safe_load(yaml_file.read_text())
            if data is None:
                continue
            case = BenchmarkCase(**data)
            result[agent_dir.name].append((yaml_file.name, case))
    return result


class TestAllCaseFilesParse:
    def test_all_case_files_parse(self):
        """Every .yaml in benchmark_cases/**/ loads as BenchmarkCase without error."""
        all_cases = _load_all_cases()
        assert len(all_cases) > 0, "No benchmark case directories found"
        total = sum(len(cases) for cases in all_cases.values())
        assert total > 0, "No benchmark case files found"

    def test_all_cases_have_query(self):
        """Every case has a non-empty query field."""
        for agent_id, cases in _load_all_cases().items():
            for filename, case in cases:
                assert case.query.strip(), f"{agent_id}/{filename}: query is empty"

    def test_all_cases_have_expected_answer(self):
        """Every case has expected_answer_contains with >=1 keyword."""
        for agent_id, cases in _load_all_cases().items():
            for filename, case in cases:
                assert len(case.expected_answer_contains) >= 1, (
                    f"{agent_id}/{filename}: expected_answer_contains is empty"
                )

    def test_no_duplicate_queries_within_agent(self):
        """No two cases within the same agent dir have identical queries."""
        for agent_id, cases in _load_all_cases().items():
            queries = [c.query for _, c in cases]
            dupes = [q for q in queries if queries.count(q) > 1]
            assert not dupes, f"{agent_id}: duplicate queries found: {set(dupes)}"

    def test_baseline_mirrors_exist(self):
        """Every specialized agent case has a matching case in unified_agent/."""
        all_cases = _load_all_cases()
        unified_queries = {c.query for _, c in all_cases.get("unified_agent", [])}
        for agent_id, cases in all_cases.items():
            if agent_id == "unified_agent":
                continue
            for filename, case in cases:
                assert case.query in unified_queries, (
                    f"{agent_id}/{filename}: query '{case.query}' has no matching baseline in unified_agent/"
                )
