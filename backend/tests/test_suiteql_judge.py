"""Tests for SuiteQL Judge Model — post-execution query verification.

Tests the judge verdict parsing and fail-open behavior without real API calls.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestParseVerdict:
    """Test _parse_verdict() directly — no mocking needed."""

    def test_parse_verdict_approved(self):
        from app.services.suiteql_judge import _parse_verdict

        raw = "APPROVED: true\nCONFIDENCE: 0.9\nREASON: Query correctly retrieves sales orders"
        verdict = _parse_verdict(raw)
        assert verdict.approved is True
        assert verdict.confidence == pytest.approx(0.9)
        assert "correctly" in verdict.reason.lower()

    def test_parse_verdict_rejected(self):
        from app.services.suiteql_judge import _parse_verdict

        raw = "APPROVED: false\nCONFIDENCE: 0.3\nREASON: wrong columns selected"
        verdict = _parse_verdict(raw)
        assert verdict.approved is False
        assert verdict.confidence == pytest.approx(0.3)
        assert "wrong columns" in verdict.reason.lower()

    def test_parse_verdict_malformed(self):
        from app.services.suiteql_judge import _parse_verdict

        raw = "This is not a valid verdict response at all."
        verdict = _parse_verdict(raw)
        # Malformed → fail-open: approved=True
        assert verdict.approved is True
        assert verdict.confidence == 0.0
        assert "parse" in verdict.reason.lower() or "malformed" in verdict.reason.lower()


class TestJudgeFailOpen:
    """Test that judge_suiteql_result() fails open on timeout/error."""

    @pytest.mark.asyncio
    async def test_judge_returns_approved_on_timeout(self):
        from app.services.suiteql_judge import judge_suiteql_result

        mock_client = MagicMock()
        mock_messages = MagicMock()

        async def slow_create(**kwargs):
            await asyncio.sleep(10)  # Will exceed timeout

        mock_messages.create = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_client.messages = mock_messages

        with patch("app.services.suiteql_judge._get_anthropic_client", return_value=mock_client):
            verdict = await judge_suiteql_result(
                user_question="How many sales orders?",
                sql="SELECT COUNT(*) FROM transaction",
                result_preview=[{"cnt": 42}],
                row_count=1,
            )

        assert verdict.approved is True
        assert "timeout" in verdict.reason.lower() or "fail-open" in verdict.reason.lower()

    @pytest.mark.asyncio
    async def test_judge_returns_approved_on_error(self):
        from app.services.suiteql_judge import judge_suiteql_result

        mock_client = MagicMock()
        mock_messages = MagicMock()
        mock_messages.create = AsyncMock(side_effect=RuntimeError("API down"))
        mock_client.messages = mock_messages

        with patch("app.services.suiteql_judge._get_anthropic_client", return_value=mock_client):
            verdict = await judge_suiteql_result(
                user_question="Show me all vendors",
                sql="SELECT * FROM vendor",
                result_preview=[{"id": 1, "companyname": "Acme"}],
                row_count=5,
            )

        assert verdict.approved is True
        assert "error" in verdict.reason.lower() or "fail-open" in verdict.reason.lower()
