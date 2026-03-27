"""Tests for the autonomous eval case generator."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.eval_case_generator import generate_eval_cases


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(count: int = 0, existing_questions: list[str] | None = None) -> MagicMock:
    """Build a mock AsyncSession whose execute() returns appropriate results."""
    db = MagicMock()

    # First execute() call → count query result
    count_scalar_result = MagicMock()
    count_scalar_result.scalar.return_value = count

    # Second execute() call → existing questions result
    existing_questions = existing_questions or []
    existing_rows = [(q,) for q in existing_questions]
    existing_result = MagicMock()
    existing_result.all.return_value = existing_rows

    # execute is called twice in the normal path; side_effect cycles through them
    db.execute = AsyncMock(side_effect=[count_scalar_result, existing_result])
    db.add = MagicMock()
    return db


_VALID_HAIKU_RESPONSE = json.dumps([
    {
        "question": "What is the total revenue from sales orders in Q1 2025?",
        "expected_keywords": ["revenue", "sales_order", "q1"],
        "expected_sql_contains": ["SELECT", "SUM", "transaction"],
    },
    {
        "question": "Show me open vendor bills pending approval by subsidiary",
        "expected_keywords": ["vendor_bill", "pending", "subsidiary"],
        "expected_sql_contains": ["SELECT", "vendorbill", "subsidiary"],
    },
    {
        "question": "What are the top 10 customers by order count in the last 90 days?",
        "expected_keywords": ["customer", "order_count", "90 days"],
        "expected_sql_contains": ["SELECT", "COUNT", "entity"],
    },
])

_TENANT_ID = uuid.UUID("ce3dfaad-626f-4992-84e9-500c8291ca0a")


# ---------------------------------------------------------------------------
# test_generate_returns_list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_returns_list():
    """Mock Haiku to return 3 valid cases; verify list of 3 dicts with required keys."""
    db = _make_db(count=0)

    import app.services.query_experiment_service as qes
    original_suiteql = getattr(qes, "_SUITEQL_SCHEMA_HINT", None)
    original_bq = getattr(qes, "_BIGQUERY_SCHEMA_HINT", None)
    try:
        qes._SUITEQL_SCHEMA_HINT = "mock suiteql schema"
        qes._BIGQUERY_SCHEMA_HINT = "mock bigquery schema"
        with patch(
            "app.services.eval_case_generator._call_haiku",
            new=AsyncMock(return_value=_VALID_HAIKU_RESPONSE),
        ):
            result = await generate_eval_cases(db, _TENANT_ID, "suiteql", max_new=5)
    finally:
        if original_suiteql is not None:
            qes._SUITEQL_SCHEMA_HINT = original_suiteql
        if original_bq is not None:
            qes._BIGQUERY_SCHEMA_HINT = original_bq

    assert isinstance(result, list)
    assert len(result) == 3
    for item in result:
        assert "question" in item
        assert "dialect" in item
        assert "expected_keywords" in item
        assert item["dialect"] == "suiteql"


# ---------------------------------------------------------------------------
# test_dedup_skips_existing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_skips_existing():
    """Haiku returns a question that matches an existing one — should be filtered out."""
    # The first generated question closely matches this existing one
    existing = ["What is the total revenue from sales orders in Q1 2025?"]
    db = _make_db(count=0, existing_questions=existing)

    import app.services.query_experiment_service as qes
    original_suiteql = getattr(qes, "_SUITEQL_SCHEMA_HINT", None)
    original_bq = getattr(qes, "_BIGQUERY_SCHEMA_HINT", None)
    try:
        qes._SUITEQL_SCHEMA_HINT = "mock suiteql schema"
        qes._BIGQUERY_SCHEMA_HINT = "mock bigquery schema"
        with patch(
            "app.services.eval_case_generator._call_haiku",
            new=AsyncMock(return_value=_VALID_HAIKU_RESPONSE),
        ):
            result = await generate_eval_cases(db, _TENANT_ID, "suiteql", max_new=5)
    finally:
        if original_suiteql is not None:
            qes._SUITEQL_SCHEMA_HINT = original_suiteql
        if original_bq is not None:
            qes._BIGQUERY_SCHEMA_HINT = original_bq

    # First case should be deduped; the other 2 should pass through
    assert isinstance(result, list)
    assert len(result) == 2
    questions = [r["question"] for r in result]
    assert not any("Q1 2025" in q and "total revenue" in q.lower() for q in questions)


# ---------------------------------------------------------------------------
# test_haiku_failure_returns_empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_haiku_failure_returns_empty():
    """When _call_haiku raises an exception, generate_eval_cases returns []."""
    db = _make_db(count=0)

    import app.services.query_experiment_service as qes
    original_suiteql = getattr(qes, "_SUITEQL_SCHEMA_HINT", None)
    original_bq = getattr(qes, "_BIGQUERY_SCHEMA_HINT", None)
    try:
        qes._SUITEQL_SCHEMA_HINT = "mock suiteql schema"
        qes._BIGQUERY_SCHEMA_HINT = "mock bigquery schema"
        with patch(
            "app.services.eval_case_generator._call_haiku",
            new=AsyncMock(side_effect=Exception("API error")),
        ):
            result = await generate_eval_cases(db, _TENANT_ID, "suiteql", max_new=5)
    finally:
        if original_suiteql is not None:
            qes._SUITEQL_SCHEMA_HINT = original_suiteql
        if original_bq is not None:
            qes._BIGQUERY_SCHEMA_HINT = original_bq

    assert result == []


# ---------------------------------------------------------------------------
# test_max_cap_respected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_cap_respected():
    """When the DB already has 200 generated cases, returns empty immediately."""
    db = _make_db(count=200)

    import app.services.query_experiment_service as qes
    original_suiteql = getattr(qes, "_SUITEQL_SCHEMA_HINT", None)
    original_bq = getattr(qes, "_BIGQUERY_SCHEMA_HINT", None)
    try:
        qes._SUITEQL_SCHEMA_HINT = "mock suiteql schema"
        qes._BIGQUERY_SCHEMA_HINT = "mock bigquery schema"
        with patch(
            "app.services.eval_case_generator._call_haiku",
            new=AsyncMock(return_value=_VALID_HAIKU_RESPONSE),
        ) as mock_haiku:
            result = await generate_eval_cases(db, _TENANT_ID, "suiteql", max_new=5)
            # Haiku must NOT be called when at cap
            mock_haiku.assert_not_called()
    finally:
        if original_suiteql is not None:
            qes._SUITEQL_SCHEMA_HINT = original_suiteql
        if original_bq is not None:
            qes._BIGQUERY_SCHEMA_HINT = original_bq

    assert result == []


# ---------------------------------------------------------------------------
# test_invalid_json_returns_empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_json_returns_empty():
    """When Haiku returns non-JSON text, generate_eval_cases returns []."""
    db = _make_db(count=0)

    import app.services.query_experiment_service as qes
    original_suiteql = getattr(qes, "_SUITEQL_SCHEMA_HINT", None)
    original_bq = getattr(qes, "_BIGQUERY_SCHEMA_HINT", None)
    try:
        qes._SUITEQL_SCHEMA_HINT = "mock suiteql schema"
        qes._BIGQUERY_SCHEMA_HINT = "mock bigquery schema"
        with patch(
            "app.services.eval_case_generator._call_haiku",
            new=AsyncMock(return_value="Here are some great test cases: blah blah not json"),
        ):
            result = await generate_eval_cases(db, _TENANT_ID, "suiteql", max_new=5)
    finally:
        if original_suiteql is not None:
            qes._SUITEQL_SCHEMA_HINT = original_suiteql
        if original_bq is not None:
            qes._BIGQUERY_SCHEMA_HINT = original_bq

    assert result == []
