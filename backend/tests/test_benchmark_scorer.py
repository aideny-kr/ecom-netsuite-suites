"""Tests for the vs-MCP benchmark scorer.

Regression proof: the substring scorer must penalize "I couldn't find"
answers even when they mention the expected keywords. This was the bug
in the first real benchmark run on 2026-04-09 where an agent's wrong
answer scored 1.00 because it mentioned all 4 country names in its
explanation of why it failed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.benchmarks.scorer import (
    ScoreResult,
    _parse_judge_json,
    llm_judge_score,
    substring_score,
)

# ---------------------------------------------------------------------------
# Substring scorer
# ---------------------------------------------------------------------------


class TestSubstringScore:
    def test_all_keywords_present_no_failure_phrase(self):
        result = substring_score(
            answer_text="Switzerland had 18 orders, Norway 3, Singapore 3, New Zealand 3.",
            expected_contains=["Switzerland", "Norway", "Singapore", "New Zealand"],
        )
        assert result.score == 1.0
        assert "4/4" in result.rationale

    def test_all_keywords_present_but_failure_phrase_caps_at_half(self):
        """Regression: 2026-04-09 false-positive.

        Agent mentioned all 4 country names but only in the context of
        "I couldn't find data for Norway, Switzerland, New Zealand,
        Singapore — they don't appear in the custom field mapping."
        Substring matching gave it 1.0. Must be penalized.
        """
        failed_answer = (
            "I couldn't find sales data for Norway, Switzerland, "
            "New Zealand, or Singapore today. None of the requested "
            "countries had orders based on the custom field."
        )
        result = substring_score(
            answer_text=failed_answer,
            expected_contains=["Norway", "Switzerland", "New Zealand", "Singapore"],
        )
        assert result.score == 0.5  # capped, not the raw 1.0
        assert "failure phrase" in result.rationale.lower()

    def test_partial_keywords_plus_failure_phrase(self):
        """Answer mentions only 2/4 keywords AND has a failure phrase —
        score is the MIN of raw (0.5) and cap (0.5) = 0.5."""
        result = substring_score(
            answer_text="I couldn't find data for Norway or Switzerland.",
            expected_contains=["Norway", "Switzerland", "Singapore", "New Zealand"],
        )
        assert result.score == 0.5

    def test_no_expected_contains_returns_1(self):
        result = substring_score(answer_text="anything", expected_contains=[])
        assert result.score == 1.0

    def test_empty_answer_zero_score(self):
        result = substring_score(answer_text="", expected_contains=["Norway"])
        assert result.score == 0.0

    def test_case_insensitive_match(self):
        result = substring_score(
            answer_text="norway had 3 orders",
            expected_contains=["Norway"],
        )
        assert result.score == 1.0

    def test_multiple_failure_phrases_detected(self):
        """Any one failure phrase triggers the cap."""
        for phrase in [
            "i couldn't find the data",
            "no results found for this query",
            "returned 0 rows",
            "error occurred while executing",
            "none of the requested countries had sales",
        ]:
            result = substring_score(
                answer_text=f"{phrase} — Norway, Switzerland",
                expected_contains=["Norway", "Switzerland"],
            )
            assert result.score <= 0.5, f"phrase '{phrase}' should have capped the score"


# ---------------------------------------------------------------------------
# LLM judge — with mocked Anthropic client
# ---------------------------------------------------------------------------


class _FakeTextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeMessage:
    def __init__(self, text: str):
        self.content = [_FakeTextBlock(text)]


def _make_fake_client(response_text: str):
    client = AsyncMock()
    client.messages = AsyncMock()
    client.messages.create = AsyncMock(return_value=_FakeMessage(response_text))
    return client


class TestLLMJudgeScore:
    @pytest.mark.asyncio
    async def test_judge_returns_high_score_for_correct_answer(self):
        """Judge should score a correct answer close to 1.0."""
        judge_response = '{"score": 0.95, "rationale": "answered with specific numbers", "correct": true}'
        with patch("app.services.benchmarks.scorer.AsyncAnthropic") as mock_anthropic:
            mock_anthropic.return_value = _make_fake_client(judge_response)
            result = await llm_judge_score(
                question="sales by country today",
                answer_text="Switzerland 18 orders $26,689. Norway 3 orders $3,116.",
                expected_contains=["Switzerland", "Norway"],
            )
        assert result.source == "llm_judge"
        assert result.score == 0.95

    @pytest.mark.asyncio
    async def test_judge_returns_low_score_for_failure_answer(self):
        """Judge should penalize 'I couldn't find' answers even if they
        mention the keywords."""
        judge_response = '{"score": 0.15, "rationale": "agent hallucinated zero results", "correct": false}'
        with patch("app.services.benchmarks.scorer.AsyncAnthropic") as mock_anthropic:
            mock_anthropic.return_value = _make_fake_client(judge_response)
            result = await llm_judge_score(
                question="sales for Norway, Switzerland, NZ, Singapore today",
                answer_text="I couldn't find data for Norway, Switzerland, New Zealand, Singapore.",
                expected_contains=["Norway", "Switzerland", "Singapore", "New Zealand"],
            )
        assert result.source == "llm_judge"
        assert result.score < 0.3

    @pytest.mark.asyncio
    async def test_judge_falls_back_on_api_error(self):
        """If Anthropic API errors, fall back to substring scoring so we
        still get a score — never crash the benchmark."""
        client = AsyncMock()
        client.messages = AsyncMock()
        client.messages.create = AsyncMock(side_effect=Exception("rate limited"))
        with patch("app.services.benchmarks.scorer.AsyncAnthropic") as mock_anthropic:
            mock_anthropic.return_value = client
            result = await llm_judge_score(
                question="q",
                answer_text="Norway had 3 orders",
                expected_contains=["Norway"],
            )
        assert result.source == "llm_judge_fallback"
        assert result.score == 1.0  # substring matched

    @pytest.mark.asyncio
    async def test_judge_parses_markdown_fenced_json(self):
        """Judge sometimes wraps JSON in ``` fences — must still parse."""
        judge_response = '```json\n{"score": 0.7, "rationale": "mostly correct", "correct": true}\n```'
        with patch("app.services.benchmarks.scorer.AsyncAnthropic") as mock_anthropic:
            mock_anthropic.return_value = _make_fake_client(judge_response)
            result = await llm_judge_score(
                question="q",
                answer_text="partial answer",
                expected_contains=[],
            )
        assert result.score == 0.7

    @pytest.mark.asyncio
    async def test_empty_answer_scores_zero_without_calling_api(self):
        """Don't spend API budget on empty answers."""
        result = await llm_judge_score(
            question="q",
            answer_text="",
            expected_contains=["Norway"],
        )
        assert result.score == 0.0
        assert result.source == "llm_judge"


# ---------------------------------------------------------------------------
# JSON parser
# ---------------------------------------------------------------------------


class TestParseJudgeJson:
    def test_plain_json(self):
        result = _parse_judge_json('{"score": 0.8, "rationale": "good"}')
        assert result == {"score": 0.8, "rationale": "good"}

    def test_markdown_fenced(self):
        result = _parse_judge_json('```json\n{"score": 0.5}\n```')
        assert result == {"score": 0.5}

    def test_extra_text_before_json(self):
        result = _parse_judge_json('Here is my evaluation: {"score": 0.9}')
        assert result == {"score": 0.9}

    def test_malformed_returns_none(self):
        assert _parse_judge_json("not json at all") is None

    def test_empty_returns_none(self):
        assert _parse_judge_json("") is None


# ---------------------------------------------------------------------------
# ScoreResult dataclass
# ---------------------------------------------------------------------------


def test_score_result_holds_score_rationale_source():
    r = ScoreResult(score=0.75, rationale="partial match", source="substring")
    assert r.score == 0.75
    assert r.rationale == "partial match"
    assert r.source == "substring"
