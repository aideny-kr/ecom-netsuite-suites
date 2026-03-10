"""Structured confidence extraction for AI chat responses.

Extracts a confidence score (1-5) from an assistant response using a
three-tier strategy:
1. Regex: free, instant extraction from <confidence>N</confidence> tags
2. Haiku: structured LLM call for missing tags (2s timeout)
3. Default: score=3 on any error or timeout
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

EXTRACTOR_MODEL = "claude-haiku-4-5-20251001"
_CONFIDENCE_RE = re.compile(r"<confidence>(\d)</confidence>")
_HAIKU_TIMEOUT_SECONDS = 2


@dataclass
class ConfidenceAssessment:
    """Result of confidence extraction."""

    score: int  # 1-5
    reasoning: str
    source: str  # "structured", "regex_fallback", "default"


def _get_anthropic_client():
    """Lazy-load the async Anthropic client."""
    import anthropic

    from app.core.config import settings

    return anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)


async def extract_structured_confidence(
    user_question: str,
    assistant_response: str,
    tools_used: list[str],
    tool_success_rate: float,
) -> ConfidenceAssessment:
    """Extract confidence from an assistant response.

    Strategy:
    1. Try regex extraction from <confidence>N</confidence> tag (free).
    2. If no tag, call Haiku for structured JSON extraction.
    3. On any error/timeout, return default score=3.
    """
    # --- Tier 1: Regex ---
    match = _CONFIDENCE_RE.search(assistant_response)
    if match:
        score = int(match.group(1))
        score = max(1, min(5, score))
        return ConfidenceAssessment(
            score=score,
            reasoning="extracted from confidence tag",
            source="regex_fallback",
        )

    # --- Tier 2: Haiku structured extraction ---
    try:
        client = _get_anthropic_client()
        prompt = (
            "You are a confidence assessor. Given a user question, an AI assistant's response, "
            "the tools used, and the tool success rate, rate the confidence of the response.\n\n"
            f"User question: {user_question}\n"
            f"Assistant response (truncated): {assistant_response[:500]}\n"
            f"Tools used: {', '.join(tools_used) if tools_used else 'none'}\n"
            f"Tool success rate: {tool_success_rate:.0%}\n\n"
            'Respond with JSON only: {"score": N, "reasoning": "one sentence"}\n'
            "Score must be 1-5 where 1=no confidence, 5=fully confident."
        )

        response = await asyncio.wait_for(
            client.messages.create(
                model=EXTRACTOR_MODEL,
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=_HAIKU_TIMEOUT_SECONDS,
        )

        text = response.content[0].text
        parsed = json.loads(text)
        score = int(parsed["score"])
        score = max(1, min(5, score))
        reasoning = parsed.get("reasoning", "")

        return ConfidenceAssessment(
            score=score,
            reasoning=reasoning,
            source="structured",
        )

    except (asyncio.TimeoutError, Exception) as exc:
        logger.warning("confidence_extractor.fallback reason=%s", str(exc))
        return ConfidenceAssessment(
            score=3,
            reasoning="extraction failed, using default",
            source="default",
        )
