"""Tests for eval_case_miner service."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from app.services.eval_case_miner import (
    _detect_dialect,
    _fallback_keywords,
    _is_duplicate,
    _extract_keywords_via_haiku,
)


# ---------------------------------------------------------------------------
# _detect_dialect
# ---------------------------------------------------------------------------


class TestDetectDialect:
    def test_netsuite_suiteql_underscore(self):
        assert _detect_dialect("netsuite_suiteql") == "suiteql"

    def test_netsuite_suiteql_dot(self):
        assert _detect_dialect("netsuite.suiteql") == "suiteql"

    def test_bigquery_sql_underscore(self):
        assert _detect_dialect("bigquery_sql") == "bigquery"

    def test_bigquery_sql_dot(self):
        assert _detect_dialect("bigquery.sql") == "bigquery"

    def test_unknown_tool(self):
        assert _detect_dialect("ns_runSavedSearch") is None

    def test_empty_string(self):
        assert _detect_dialect("") is None

    def test_case_insensitive(self):
        assert _detect_dialect("NetSuite_SuiteQL") == "suiteql"
        assert _detect_dialect("BigQuery_SQL") == "bigquery"


# ---------------------------------------------------------------------------
# _is_duplicate
# ---------------------------------------------------------------------------


class TestIsDuplicate:
    def test_exact_match(self):
        question = "Show me all open sales orders"
        assert _is_duplicate(question, [question]) is True

    def test_similar_above_threshold(self):
        # Most non-stopwords overlap
        q1 = "List all open sales orders for customer Acme Corp"
        q2 = "Show all open sales orders for customer Acme Corp"
        assert _is_duplicate(q1, [q2]) is True

    def test_different_questions(self):
        q1 = "What is the total revenue for Q1 2025?"
        q2 = "Show me all vendor bills pending approval"
        assert _is_duplicate(q1, [q2]) is False

    def test_empty_existing(self):
        assert _is_duplicate("What are my open orders?", []) is False

    def test_empty_question(self):
        # All words are stopwords or empty — tokens will be empty
        assert _is_duplicate("", ["Show me open orders"]) is False

    def test_multiple_existing_one_match(self):
        q = "Total revenue by subsidiary Q4"
        existing = [
            "Show vendor bills pending approval",
            "Total revenue by subsidiary Q4",
            "What customers have overdue balances",
        ]
        assert _is_duplicate(q, existing) is True

    def test_partial_overlap_below_threshold(self):
        # Low overlap — only 1-2 shared non-stopword tokens
        q1 = "Show me the revenue breakdown by product category for last year"
        q2 = "List all employees by department in subsidiary"
        assert _is_duplicate(q1, [q2]) is False


# ---------------------------------------------------------------------------
# _fallback_keywords
# ---------------------------------------------------------------------------


class TestFallbackKeywords:
    def test_removes_stopwords(self):
        question = "Show me all open sales orders"
        keywords = _fallback_keywords(question)
        # "Show", "me", "all", "open", "sales", "orders"
        # stopwords: "me", "all"
        assert "me" not in keywords
        assert "all" not in keywords

    def test_removes_short_words(self):
        question = "Get all SO by id or PO"
        keywords = _fallback_keywords(question)
        # "id" (2 chars) should be excluded; "or" is stopword
        assert "id" not in keywords

    def test_max_8_keywords(self):
        question = "revenue transactions customers vendors subsidiaries departments classes locations periods"
        keywords = _fallback_keywords(question)
        assert len(keywords) <= 8

    def test_deduplication(self):
        question = "sales sales orders orders by customer customer"
        keywords = _fallback_keywords(question)
        assert len(keywords) == len(set(keywords))

    def test_returns_list(self):
        result = _fallback_keywords("What are total sales by customer?")
        assert isinstance(result, list)

    def test_empty_question(self):
        assert _fallback_keywords("") == []


# ---------------------------------------------------------------------------
# _extract_keywords_via_haiku
# ---------------------------------------------------------------------------


class TestExtractKeywordsViaHaiku:
    @pytest.mark.asyncio
    async def test_returns_parsed_list_from_haiku(self):
        mock_response = '["sales_order", "status", "open", "customer", "amount"]'
        with patch(
            "app.services.eval_case_miner._call_haiku",
            new=AsyncMock(return_value=mock_response),
        ):
            result = await _extract_keywords_via_haiku(
                "Show me all open sales orders",
                '{"columns": ["id", "status"], "rows": [["1", "open"]]}',
            )
        assert result == ["sales_order", "status", "open", "customer", "amount"]

    @pytest.mark.asyncio
    async def test_returns_parsed_list_with_markdown_fence(self):
        mock_response = '```json\n["revenue", "subsidiary", "total"]\n```'
        with patch(
            "app.services.eval_case_miner._call_haiku",
            new=AsyncMock(return_value=mock_response),
        ):
            result = await _extract_keywords_via_haiku(
                "Revenue by subsidiary",
                '{"rows": [["East", 100000]]}',
            )
        assert result == ["revenue", "subsidiary", "total"]

    @pytest.mark.asyncio
    async def test_fallback_on_json_parse_error(self):
        with patch(
            "app.services.eval_case_miner._call_haiku",
            new=AsyncMock(return_value="not valid json at all"),
        ):
            result = await _extract_keywords_via_haiku(
                "Show open sales orders by customer",
                "{}",
            )
        # Should fall back to _fallback_keywords
        assert isinstance(result, list)
        # Fallback should extract domain words like "open", "sales", "orders", "customer"
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_fallback_on_haiku_exception(self):
        with patch(
            "app.services.eval_case_miner._call_haiku",
            new=AsyncMock(side_effect=Exception("API timeout")),
        ):
            result = await _extract_keywords_via_haiku(
                "List vendor bills pending approval",
                "{}",
            )
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_fallback_when_fewer_than_3_keywords_returned(self):
        # Only 2 keywords returned — should fall back
        mock_response = '["sales", "orders"]'
        with patch(
            "app.services.eval_case_miner._call_haiku",
            new=AsyncMock(return_value=mock_response),
        ):
            result = await _extract_keywords_via_haiku(
                "Show open sales orders by customer status",
                "{}",
            )
        # With fewer than 3, falls back to _fallback_keywords
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_caps_at_max_keywords(self):
        many_keywords = '["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]'
        with patch(
            "app.services.eval_case_miner._call_haiku",
            new=AsyncMock(return_value=many_keywords),
        ):
            result = await _extract_keywords_via_haiku("question", "result")
        assert len(result) <= 8
