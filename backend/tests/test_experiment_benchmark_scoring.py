"""Tests for auto-improve experiment scoring in run_single_experiment().

The nightly loop generates a candidate SQL, executes it against live NetSuite
or BigQuery, and then judges whether the returned rows answer the question
(via an LLM judge over the SQL result text). No agent loop, no baseline
comparison — that signal lives in the vs-MCP benchmark now.

KEEP  — judge score >= KEEP_THRESHOLD (0.6)
REVERT — judge score <= REVERT_THRESHOLD (0.3)
SKIP  — anything in between, or SQL generation/execution failure
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.query_eval_harness import EvalCase

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TENANT_ID = uuid.UUID("ce3dfaad-626f-4992-84e9-500c8291ca0a")
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
class _FakeScoreResult:
    score: float
    rationale: str = "test"
    source: str = "llm_judge"


def _mock_db():
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock()
    return db


def _exec_ok(result_text: str = "42 open sales orders"):
    return {
        "success": True,
        "result_text": result_text,
        "rows": 1,
        "bytes_processed": 0,
    }


def _patch_common_ok(judge_score: float):
    """Stack the usual happy-path patches with a given judge score."""
    return (
        patch(
            f"{_SVC}._generate_sql",
            new_callable=AsyncMock,
            return_value="SELECT COUNT(*) FROM transaction",
        ),
        patch(
            f"{_SVC}._execute_sql",
            new_callable=AsyncMock,
            return_value=_exec_ok(),
        ),
        patch(
            f"{_SVC}.llm_judge_score",
            new_callable=AsyncMock,
            return_value=_FakeScoreResult(score=judge_score),
        ),
        patch(f"{_SVC}.promote_experiment_result", new_callable=AsyncMock),
    )


# ---------------------------------------------------------------------------
# Tests — decision thresholds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keep_when_judge_above_threshold():
    """KEEP when judge score >= 0.6."""
    db = _mock_db()
    patches = _patch_common_ok(judge_score=0.8)
    with patches[0], patches[1], patches[2], patches[3]:
        from app.services.query_experiment_service import run_single_experiment

        result = await run_single_experiment(
            case=_make_case(), tenant_id=_TENANT_ID, db=db
        )

    assert result["decision"] == "KEEP"
    assert result["experiment_score"] == 0.8


@pytest.mark.asyncio
async def test_keep_at_exact_threshold():
    """KEEP boundary — score at exactly 0.6 promotes."""
    db = _mock_db()
    patches = _patch_common_ok(judge_score=0.6)
    with patches[0], patches[1], patches[2], patches[3]:
        from app.services.query_experiment_service import run_single_experiment

        result = await run_single_experiment(
            case=_make_case(), tenant_id=_TENANT_ID, db=db
        )

    assert result["decision"] == "KEEP"


@pytest.mark.asyncio
async def test_revert_when_judge_below_reject_threshold():
    """REVERT when judge score <= 0.3."""
    db = _mock_db()
    patches = _patch_common_ok(judge_score=0.2)
    with patches[0], patches[1], patches[2], patches[3]:
        from app.services.query_experiment_service import run_single_experiment

        result = await run_single_experiment(
            case=_make_case(), tenant_id=_TENANT_ID, db=db
        )

    assert result["decision"] == "REVERT"
    assert result["experiment_score"] == 0.2


@pytest.mark.asyncio
async def test_revert_at_exact_threshold():
    """REVERT boundary — score at exactly 0.3 reverts."""
    db = _mock_db()
    patches = _patch_common_ok(judge_score=0.3)
    with patches[0], patches[1], patches[2], patches[3]:
        from app.services.query_experiment_service import run_single_experiment

        result = await run_single_experiment(
            case=_make_case(), tenant_id=_TENANT_ID, db=db
        )

    assert result["decision"] == "REVERT"


@pytest.mark.asyncio
async def test_skip_when_judge_in_middle_band():
    """SKIP when 0.3 < score < 0.6."""
    db = _mock_db()
    patches = _patch_common_ok(judge_score=0.45)
    with patches[0], patches[1], patches[2], patches[3]:
        from app.services.query_experiment_service import run_single_experiment

        result = await run_single_experiment(
            case=_make_case(), tenant_id=_TENANT_ID, db=db
        )

    assert result["decision"] == "SKIP"
    assert result["experiment_score"] == 0.45


# ---------------------------------------------------------------------------
# Tests — failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_when_sql_generation_fails():
    """SKIP when _generate_sql returns None — judge never called."""
    db = _mock_db()
    judge_mock = AsyncMock()
    with (
        patch(f"{_SVC}._generate_sql", new_callable=AsyncMock, return_value=None),
        patch(f"{_SVC}._execute_sql", new_callable=AsyncMock),
        patch(f"{_SVC}.llm_judge_score", judge_mock),
        patch(f"{_SVC}.promote_experiment_result", new_callable=AsyncMock),
    ):
        from app.services.query_experiment_service import run_single_experiment

        result = await run_single_experiment(
            case=_make_case(), tenant_id=_TENANT_ID, db=db
        )

    assert result["decision"] == "SKIP"
    assert result["executed_successfully"] is False
    assert result["error_message"] is not None
    judge_mock.assert_not_called()


@pytest.mark.asyncio
async def test_skip_when_sql_execution_fails():
    """SKIP when _execute_sql returns success=False — judge never called."""
    db = _mock_db()
    judge_mock = AsyncMock()
    with (
        patch(f"{_SVC}._generate_sql", new_callable=AsyncMock, return_value="SELECT 1"),
        patch(
            f"{_SVC}._execute_sql",
            new_callable=AsyncMock,
            return_value={
                "success": False,
                "error": "syntax error at token FROM",
                "result_text": "",
                "rows": 0,
                "bytes_processed": 0,
            },
        ),
        patch(f"{_SVC}.llm_judge_score", judge_mock),
        patch(f"{_SVC}.promote_experiment_result", new_callable=AsyncMock),
    ):
        from app.services.query_experiment_service import run_single_experiment

        result = await run_single_experiment(
            case=_make_case(), tenant_id=_TENANT_ID, db=db
        )

    assert result["decision"] == "SKIP"
    assert result["executed_successfully"] is False
    assert "syntax error" in (result["error_message"] or "")
    judge_mock.assert_not_called()


@pytest.mark.asyncio
async def test_skip_when_judge_raises():
    """SKIP gracefully when LLM judge raises — experiment_score stays 0.0."""
    db = _mock_db()
    with (
        patch(
            f"{_SVC}._generate_sql",
            new_callable=AsyncMock,
            return_value="SELECT COUNT(*) FROM transaction",
        ),
        patch(f"{_SVC}._execute_sql", new_callable=AsyncMock, return_value=_exec_ok()),
        patch(
            f"{_SVC}.llm_judge_score",
            new_callable=AsyncMock,
            side_effect=RuntimeError("anthropic 529"),
        ),
        patch(f"{_SVC}.promote_experiment_result", new_callable=AsyncMock),
    ):
        from app.services.query_experiment_service import run_single_experiment

        result = await run_single_experiment(
            case=_make_case(), tenant_id=_TENANT_ID, db=db
        )

    assert result["decision"] == "SKIP"
    assert result["experiment_score"] == 0.0
    # Executed_successfully stays True — the SQL did run; judge just failed.
    assert result["executed_successfully"] is True


# ---------------------------------------------------------------------------
# Tests — behavioral contracts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_judge_called_with_sql_result_text_and_keywords():
    """Judge receives result_text from exec, plus question + expected_keywords."""
    db = _mock_db()
    judge_mock = AsyncMock(return_value=_FakeScoreResult(score=0.7))
    case = _make_case(
        question="How many Norway orders?",
        expected_keywords=["Norway", "orders"],
    )
    exec_rows = "country | order_count\nNorway | 27\nSweden | 12"
    with (
        patch(f"{_SVC}._generate_sql", new_callable=AsyncMock, return_value="SELECT ..."),
        patch(
            f"{_SVC}._execute_sql",
            new_callable=AsyncMock,
            return_value=_exec_ok(result_text=exec_rows),
        ),
        patch(f"{_SVC}.llm_judge_score", judge_mock),
        patch(f"{_SVC}.promote_experiment_result", new_callable=AsyncMock),
    ):
        from app.services.query_experiment_service import run_single_experiment

        await run_single_experiment(case=case, tenant_id=_TENANT_ID, db=db)

    judge_mock.assert_called_once()
    kwargs = judge_mock.call_args.kwargs
    assert kwargs["question"] == "How many Norway orders?"
    assert kwargs["answer_text"] == exec_rows
    assert kwargs["expected_contains"] == ["Norway", "orders"]


@pytest.mark.asyncio
async def test_keep_promotes_via_promote_experiment_result():
    """KEEP decision triggers promote_experiment_result with test_query set."""
    db = _mock_db()
    promote_mock = AsyncMock()
    with (
        patch(f"{_SVC}._generate_sql", new_callable=AsyncMock, return_value="SELECT 1"),
        patch(f"{_SVC}._execute_sql", new_callable=AsyncMock, return_value=_exec_ok()),
        patch(
            f"{_SVC}.llm_judge_score",
            new_callable=AsyncMock,
            return_value=_FakeScoreResult(score=0.9),
        ),
        patch(f"{_SVC}.promote_experiment_result", promote_mock),
    ):
        from app.services.query_experiment_service import run_single_experiment

        case = _make_case(question="How many open POs?")
        await run_single_experiment(case=case, tenant_id=_TENANT_ID, db=db)

    promote_mock.assert_called_once()
    passed_result = promote_mock.call_args.args[0]
    assert passed_result["decision"] == "KEEP"
    assert passed_result["test_query"] == "How many open POs?"


@pytest.mark.asyncio
async def test_return_keys_match_caller_expectations():
    """Result dict must include keys auto_query_improvement and ExperimentLog need."""
    db = _mock_db()
    patches = _patch_common_ok(judge_score=0.7)
    with patches[0], patches[1], patches[2], patches[3]:
        from app.services.query_experiment_service import run_single_experiment

        result = await run_single_experiment(
            case=_make_case(), tenant_id=_TENANT_ID, db=db
        )

    required = {
        "dialect",
        "question",
        "generated_sql",
        "executed_successfully",
        "experiment_score",
        "baseline_score",  # kept at 0.0 for ExperimentLog compatibility
        "delta",
        "decision",
        "error_message",
        "cost_usd",
    }
    assert required.issubset(result.keys()), f"Missing keys: {required - result.keys()}"
    assert isinstance(result["experiment_score"], float)
    assert isinstance(result["baseline_score"], float)
    assert result["decision"] in ("KEEP", "REVERT", "SKIP")


@pytest.mark.asyncio
async def test_no_agent_or_baseline_runner_imports():
    """Defense: the service no longer depends on run_agent / run_baseline.

    Catches accidental re-introduction of the expensive agent-loop path that
    caused the 2026-04-14 regression (12-step exhaustion, all scores 0.0).
    """
    import app.services.query_experiment_service as svc

    assert not hasattr(svc, "run_agent"), (
        "run_agent should not be imported into query_experiment_service — "
        "the experiment scorer judges the SQL result directly, not the agent loop."
    )
    assert not hasattr(svc, "run_baseline"), (
        "run_baseline should not be imported into query_experiment_service — "
        "vs-MCP baseline comparison lives in the benchmark harness, not here."
    )
