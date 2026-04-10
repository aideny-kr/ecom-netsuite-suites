"""Tests for vs-MCP benchmark scoring in run_single_experiment().

Verifies that the nightly auto-improvement loop uses the vs-MCP benchmark
(run_agent vs run_baseline, scored with substring_score) to decide
KEEP / REVERT / SKIP for candidate patterns.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.query_eval_harness import EvalCase

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TENANT_ID = uuid.UUID("ce3dfaad-626f-4992-84e9-500c8291ca0a")

# Module path for patching — all benchmark imports live in this namespace
_SVC = "app.services.query_experiment_service"


def _make_case(**overrides) -> EvalCase:
    defaults = {
        "question": "How many open sales orders are there?",
        "dialect": "suiteql",
        "expected_keywords": ["sales", "orders", "open"],
    }
    defaults.update(overrides)
    return EvalCase(**defaults)


@dataclass
class _FakeAgentResult:
    answer_text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    input_tokens: int = 100
    output_tokens: int = 200
    cost_usd: float = 0.01
    latency_ms: int = 500
    success: bool = True
    error: str | None = None
    confidence_score: float | None = None
    num_steps: int = 1
    context_chars: int = 500


@dataclass
class _FakeBaselineResult:
    answer_text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    input_tokens: int = 100
    output_tokens: int = 200
    cost_usd: float = 0.01
    latency_ms: int = 500
    success: bool = True
    error: str | None = None


def _mock_db():
    """Create a mock AsyncSession with the operations experiment service needs."""
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock()
    return db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keep_when_agent_beats_baseline():
    """KEEP: agent_score >= baseline_score AND agent_score > 0.5."""
    agent_result = _FakeAgentResult(
        # Agent gives a great answer with all 3 keywords
        answer_text="There are 42 open sales orders in your NetSuite account.",
        success=True,
    )
    baseline_result = _FakeBaselineResult(
        # Baseline fails — failure phrase caps substring_score at 0.5
        answer_text="I couldn't find the data for open sales orders.",
        success=True,
    )
    db = _mock_db()

    with (
        patch(f"{_SVC}._generate_sql", new_callable=AsyncMock, return_value="SELECT COUNT(*) FROM transaction WHERE type = 'SalesOrd'"),
        patch(f"{_SVC}._execute_sql", new_callable=AsyncMock, return_value={"success": True, "result_text": "42", "rows": 1, "bytes_processed": 0}),
        patch(f"{_SVC}.run_agent", new_callable=AsyncMock, return_value=agent_result),
        patch(f"{_SVC}.run_baseline", new_callable=AsyncMock, return_value=baseline_result),
        patch(f"{_SVC}.promote_experiment_result", new_callable=AsyncMock),
    ):
        from app.services.query_experiment_service import run_single_experiment

        result = await run_single_experiment(
            case=_make_case(),
            tenant_id=_TENANT_ID,
            db=db,
        )

    assert result["decision"] == "KEEP", (
        f"Expected KEEP, got {result['decision']} "
        f"(agent={result['experiment_score']}, baseline={result['baseline_score']})"
    )
    assert result["experiment_score"] > 0.5
    assert result["experiment_score"] >= result["baseline_score"]
    assert "delta" in result
    assert result["delta"] > 0


@pytest.mark.asyncio
async def test_revert_when_agent_worse_than_baseline():
    """REVERT: agent_score < baseline_score - 0.1 (agent got worse)."""
    agent_result = _FakeAgentResult(
        # Agent gives a terrible answer — failure phrase + missing keywords
        answer_text="I couldn't find the data. An error occurred while querying.",
        success=True,
    )
    baseline_result = _FakeBaselineResult(
        # Baseline gives a great answer with all keywords
        answer_text="There are 42 open sales orders in your NetSuite account.",
        success=True,
    )
    db = _mock_db()

    with (
        patch(f"{_SVC}._generate_sql", new_callable=AsyncMock, return_value="SELECT COUNT(*) FROM transaction"),
        patch(f"{_SVC}._execute_sql", new_callable=AsyncMock, return_value={"success": True, "result_text": "42", "rows": 1, "bytes_processed": 0}),
        patch(f"{_SVC}.run_agent", new_callable=AsyncMock, return_value=agent_result),
        patch(f"{_SVC}.run_baseline", new_callable=AsyncMock, return_value=baseline_result),
        patch(f"{_SVC}.promote_experiment_result", new_callable=AsyncMock),
    ):
        from app.services.query_experiment_service import run_single_experiment

        result = await run_single_experiment(
            case=_make_case(),
            tenant_id=_TENANT_ID,
            db=db,
        )

    assert result["decision"] == "REVERT", (
        f"Expected REVERT, got {result['decision']} "
        f"(agent={result['experiment_score']}, baseline={result['baseline_score']})"
    )
    assert result["experiment_score"] < result["baseline_score"] - 0.1


@pytest.mark.asyncio
async def test_skip_when_scores_are_close_and_low():
    """SKIP: agent barely beats baseline but both score <= 0.5 (low quality).

    The SKIP region is: agent >= baseline (so not REVERT) but agent <= 0.5
    (so not KEEP). This tests the boundary where both systems perform poorly
    on a question — we don't want to promote a pattern just because the
    agent's bad answer was slightly less bad than the baseline's.
    """
    # expected_keywords: ["sales", "orders", "open", "pending", "count"]
    # Agent mentions 2/5 => 0.4, baseline mentions 1/5 => 0.2
    case = _make_case(expected_keywords=["sales", "orders", "open", "pending", "count"])
    agent_result = _FakeAgentResult(
        answer_text="I see some sales orders but the query timed out.",
        success=True,
    )
    baseline_result = _FakeBaselineResult(
        answer_text="Here are some orders from the system.",
        success=True,
    )
    db = _mock_db()

    with (
        patch(f"{_SVC}._generate_sql", new_callable=AsyncMock, return_value="SELECT COUNT(*) FROM transaction"),
        patch(f"{_SVC}._execute_sql", new_callable=AsyncMock, return_value={"success": True, "result_text": "42", "rows": 1, "bytes_processed": 0}),
        patch(f"{_SVC}.run_agent", new_callable=AsyncMock, return_value=agent_result),
        patch(f"{_SVC}.run_baseline", new_callable=AsyncMock, return_value=baseline_result),
        patch(f"{_SVC}.promote_experiment_result", new_callable=AsyncMock),
    ):
        from app.services.query_experiment_service import run_single_experiment

        result = await run_single_experiment(
            case=case,
            tenant_id=_TENANT_ID,
            db=db,
        )

    # Agent score ~0.4, baseline ~0.2. Agent >= baseline but agent <= 0.5 => SKIP
    assert result["decision"] == "SKIP", (
        f"Expected SKIP, got {result['decision']} "
        f"(agent={result['experiment_score']}, baseline={result['baseline_score']})"
    )
    assert result["experiment_score"] <= 0.5
    assert result["experiment_score"] >= result["baseline_score"]


@pytest.mark.asyncio
async def test_skip_when_agent_above_baseline_but_below_threshold():
    """SKIP: agent >= baseline but agent_score <= 0.5 (low quality overall)."""
    agent_result = _FakeAgentResult(
        # Agent mentions only 1 of 3 keywords — 0.333 score
        answer_text="Something about orders in the system.",
        success=True,
    )
    baseline_result = _FakeBaselineResult(
        # Baseline mentions zero keywords
        answer_text="I have no information about that topic.",
        success=True,
    )
    db = _mock_db()

    with (
        patch(f"{_SVC}._generate_sql", new_callable=AsyncMock, return_value="SELECT COUNT(*) FROM transaction"),
        patch(f"{_SVC}._execute_sql", new_callable=AsyncMock, return_value={"success": True, "result_text": "42", "rows": 1, "bytes_processed": 0}),
        patch(f"{_SVC}.run_agent", new_callable=AsyncMock, return_value=agent_result),
        patch(f"{_SVC}.run_baseline", new_callable=AsyncMock, return_value=baseline_result),
        patch(f"{_SVC}.promote_experiment_result", new_callable=AsyncMock),
    ):
        from app.services.query_experiment_service import run_single_experiment

        result = await run_single_experiment(
            case=_make_case(),
            tenant_id=_TENANT_ID,
            db=db,
        )

    assert result["decision"] == "SKIP", (
        f"Expected SKIP, got {result['decision']} "
        f"(agent={result['experiment_score']}, baseline={result['baseline_score']})"
    )
    assert result["experiment_score"] <= 0.5


@pytest.mark.asyncio
async def test_skip_when_benchmark_runners_fail():
    """SKIP gracefully when run_agent or run_baseline return failures."""
    agent_result = _FakeAgentResult(success=False, error="connection_failed")
    baseline_result = _FakeBaselineResult(success=False, error="timeout")

    db = _mock_db()

    with (
        patch(f"{_SVC}._generate_sql", new_callable=AsyncMock, return_value="SELECT COUNT(*) FROM transaction"),
        patch(f"{_SVC}._execute_sql", new_callable=AsyncMock, return_value={"success": True, "result_text": "42", "rows": 1, "bytes_processed": 0}),
        patch(f"{_SVC}.run_agent", new_callable=AsyncMock, return_value=agent_result),
        patch(f"{_SVC}.run_baseline", new_callable=AsyncMock, return_value=baseline_result),
        patch(f"{_SVC}.promote_experiment_result", new_callable=AsyncMock),
    ):
        from app.services.query_experiment_service import run_single_experiment

        result = await run_single_experiment(
            case=_make_case(),
            tenant_id=_TENANT_ID,
            db=db,
        )

    # When both runners fail (success=False), scores stay at 0.0
    assert result["decision"] == "SKIP"
    assert result["experiment_score"] == 0.0
    assert result["baseline_score"] == 0.0


@pytest.mark.asyncio
async def test_return_keys_match_caller_expectations():
    """Verify the result dict contains all keys auto_query_improvement expects."""
    agent_result = _FakeAgentResult(
        answer_text="There are 42 open sales orders.",
        success=True,
    )
    baseline_result = _FakeBaselineResult(
        answer_text="I found open sales orders.",
        success=True,
    )
    db = _mock_db()

    with (
        patch(f"{_SVC}._generate_sql", new_callable=AsyncMock, return_value="SELECT COUNT(*) FROM transaction"),
        patch(f"{_SVC}._execute_sql", new_callable=AsyncMock, return_value={"success": True, "result_text": "42", "rows": 1, "bytes_processed": 0}),
        patch(f"{_SVC}.run_agent", new_callable=AsyncMock, return_value=agent_result),
        patch(f"{_SVC}.run_baseline", new_callable=AsyncMock, return_value=baseline_result),
        patch(f"{_SVC}.promote_experiment_result", new_callable=AsyncMock),
    ):
        from app.services.query_experiment_service import run_single_experiment

        result = await run_single_experiment(
            case=_make_case(),
            tenant_id=_TENANT_ID,
            db=db,
        )

    # These keys are expected by auto_query_improvement.py
    required_keys = {
        "dialect",
        "question",
        "generated_sql",
        "executed_successfully",
        "experiment_score",
        "baseline_score",
        "delta",
        "decision",
        "error_message",
        "cost_usd",
    }
    assert required_keys.issubset(result.keys()), f"Missing keys: {required_keys - result.keys()}"
    assert isinstance(result["experiment_score"], float)
    assert isinstance(result["baseline_score"], float)
    assert isinstance(result["delta"], float)
    assert result["decision"] in ("KEEP", "REVERT", "SKIP")
