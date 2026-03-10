"""Tests for structured confidence extraction.

Tests the ConfidenceAssessment dataclass and extract_structured_confidence()
function, verifying regex fallback, Haiku structured extraction, error handling,
timeout handling, and score clamping.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.confidence_extractor import ConfidenceAssessment, extract_structured_confidence


@pytest.mark.asyncio
async def test_extracts_confidence_from_regex_tag():
    """When response contains <confidence>N</confidence>, return score from regex without calling Haiku."""
    assessment = await extract_structured_confidence(
        user_question="How many orders last week?",
        assistant_response="Here are 42 orders. <confidence>4</confidence>",
        tools_used=["netsuite_suiteql"],
        tool_success_rate=1.0,
    )
    assert assessment.score == 4
    assert assessment.source == "regex_fallback"
    assert isinstance(assessment, ConfidenceAssessment)


@pytest.mark.asyncio
async def test_extracts_confidence_via_haiku_when_no_tag():
    """When no regex tag, call Haiku for structured extraction."""
    mock_response = MagicMock()
    mock_content_block = MagicMock()
    mock_content_block.text = '{"score": 3, "reasoning": "partial data"}'
    mock_response.content = [mock_content_block]

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("app.services.confidence_extractor._get_anthropic_client", return_value=mock_client):
        assessment = await extract_structured_confidence(
            user_question="Show me revenue by region",
            assistant_response="Revenue data is incomplete due to missing regions.",
            tools_used=["netsuite_suiteql"],
            tool_success_rate=0.5,
        )

    assert assessment.score == 3
    assert assessment.source == "structured"
    assert assessment.reasoning == "partial data"
    mock_client.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_fallback_on_haiku_error():
    """When Haiku raises an exception, return default score=3."""
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API error"))

    with patch("app.services.confidence_extractor._get_anthropic_client", return_value=mock_client):
        assessment = await extract_structured_confidence(
            user_question="What is the total?",
            assistant_response="The total is $1000.",
            tools_used=[],
            tool_success_rate=0.0,
        )

    assert assessment.score == 3
    assert assessment.source == "default"


@pytest.mark.asyncio
async def test_fallback_on_timeout():
    """When Haiku times out, return default score=3."""
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=asyncio.TimeoutError())

    with patch("app.services.confidence_extractor._get_anthropic_client", return_value=mock_client):
        assessment = await extract_structured_confidence(
            user_question="Show inventory",
            assistant_response="Here is the inventory list.",
            tools_used=["netsuite_suiteql"],
            tool_success_rate=1.0,
        )

    assert assessment.score == 3
    assert assessment.source == "default"


@pytest.mark.asyncio
async def test_score_clamped_to_1_5():
    """When Haiku returns score > 5, clamp to 5."""
    mock_response = MagicMock()
    mock_content_block = MagicMock()
    mock_content_block.text = '{"score": 10, "reasoning": "very confident"}'
    mock_response.content = [mock_content_block]

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("app.services.confidence_extractor._get_anthropic_client", return_value=mock_client):
        assessment = await extract_structured_confidence(
            user_question="How many items?",
            assistant_response="There are 100 items.",
            tools_used=["netsuite_suiteql"],
            tool_success_rate=1.0,
        )

    assert assessment.score == 5
    assert assessment.source == "structured"
