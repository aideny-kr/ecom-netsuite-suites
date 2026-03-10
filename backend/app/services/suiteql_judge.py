"""SuiteQL Judge Model — post-execution query verification.

A lightweight judge that validates SuiteQL query results using Haiku
after execution. Designed to catch cases where the query technically
succeeds but doesn't correctly answer the user's question.

Fail-open on timeout or error: if the judge can't run, we approve.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.importance_classifier import ImportanceTier

import anthropic

from app.core.config import settings

_logger = logging.getLogger(__name__)

JUDGE_MODEL = "claude-haiku-4-5-20251001"
JUDGE_TIMEOUT_SECONDS = 3

_JUDGE_PROMPT = """\
You are a SuiteQL query result validator. Given a user's question, the SQL query that was executed, \
and a preview of the results, determine whether the query correctly addresses the question.

Check:
1. Does the query address the user's question?
2. Are the results sensible (not empty when data is expected, not obviously wrong)?
3. Are the selected columns relevant to the question?
4. If the question asks for aggregation (totals, counts, averages), does the query use GROUP BY?

Respond in EXACTLY this format (three lines, no extra text):
APPROVED: true/false
CONFIDENCE: 0.0-1.0
REASON: one-line explanation
"""


@dataclass(frozen=True)
class JudgeVerdict:
    """Result of the judge's evaluation."""

    approved: bool
    confidence: float
    reason: str


def _get_anthropic_client() -> anthropic.AsyncAnthropic:
    """Create an Anthropic async client. Separated for easy mocking."""
    return anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)


def _parse_verdict(raw: str) -> JudgeVerdict:
    """Parse the judge's structured response into a JudgeVerdict.

    If the response is malformed, returns an approved verdict (fail-open).
    """
    try:
        approved_match = re.search(r"APPROVED:\s*(true|false)", raw, re.IGNORECASE)
        confidence_match = re.search(r"CONFIDENCE:\s*([\d.]+)", raw, re.IGNORECASE)
        reason_match = re.search(r"REASON:\s*(.+)", raw, re.IGNORECASE)

        if not approved_match:
            return JudgeVerdict(
                approved=True,
                confidence=0.0,
                reason="Malformed judge response — fail-open",
            )

        approved = approved_match.group(1).lower() == "true"
        confidence = float(confidence_match.group(1)) if confidence_match else 0.0
        confidence = max(0.0, min(1.0, confidence))
        reason = reason_match.group(1).strip() if reason_match else "No reason provided"

        return JudgeVerdict(approved=approved, confidence=confidence, reason=reason)
    except Exception:
        return JudgeVerdict(
            approved=True,
            confidence=0.0,
            reason="Parse error in judge response — fail-open",
        )


async def judge_suiteql_result(
    *,
    user_question: str,
    sql: str,
    result_preview: list[Any],
    row_count: int,
) -> JudgeVerdict:
    """Call Haiku to validate whether a SuiteQL result answers the user's question.

    Fail-open: returns approved=True on timeout or any error.

    Args:
        user_question: The original user question.
        sql: The SuiteQL query that was executed.
        result_preview: First few rows of the result (list of dicts or lists).
        row_count: Total number of rows returned.

    Returns:
        JudgeVerdict with approved, confidence, and reason.
    """
    user_message = (
        f"User question: {user_question}\n\n"
        f"SQL query:\n{sql}\n\n"
        f"Result preview ({row_count} total rows):\n{result_preview}\n"
    )

    try:
        client = _get_anthropic_client()
        response = await asyncio.wait_for(
            client.messages.create(
                model=JUDGE_MODEL,
                max_tokens=150,
                system=_JUDGE_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            ),
            timeout=JUDGE_TIMEOUT_SECONDS,
        )
        raw_text = response.content[0].text
        verdict = _parse_verdict(raw_text)
        _logger.info(
            "suiteql_judge.verdict",
            extra={
                "approved": verdict.approved,
                "confidence": verdict.confidence,
                "reason": verdict.reason,
            },
        )
        return verdict

    except asyncio.TimeoutError:
        _logger.warning("suiteql_judge.timeout — fail-open")
        return JudgeVerdict(
            approved=True,
            confidence=0.0,
            reason="Judge timeout — fail-open",
        )
    except Exception as exc:
        _logger.warning("suiteql_judge.error — fail-open: %s", exc)
        return JudgeVerdict(
            approved=True,
            confidence=0.0,
            reason=f"Judge error — fail-open: {exc}",
        )


@dataclass(frozen=True)
class EnforcementResult:
    """Result of applying tier-specific confidence thresholds."""

    passed: bool
    tier: str
    needs_review: bool
    reason: str


def enforce_judge_threshold(
    verdict: JudgeVerdict,
    tier: "ImportanceTier",
) -> EnforcementResult:
    """Apply tier-specific confidence thresholds to a judge verdict."""
    from app.services.importance_classifier import ImportanceTier

    threshold = tier.judge_confidence_threshold

    # Casual tier: always pass (existing fail-open behavior)
    if tier == ImportanceTier.CASUAL:
        return EnforcementResult(
            passed=True, tier=tier.label, needs_review=False, reason=verdict.reason
        )

    # Tier 2+: disapproved verdict always fails
    if not verdict.approved:
        return EnforcementResult(
            passed=False,
            tier=tier.label,
            needs_review=tier == ImportanceTier.AUDIT_CRITICAL,
            reason=f"Judge disapproved: {verdict.reason}",
        )

    # Check confidence threshold
    passed = verdict.confidence >= threshold
    needs_review = not passed and tier == ImportanceTier.AUDIT_CRITICAL

    if passed:
        reason = verdict.reason
    else:
        reason = f"Confidence {verdict.confidence:.2f} below threshold {threshold:.2f} for {tier.label} tier"

    return EnforcementResult(
        passed=passed, tier=tier.label, needs_review=needs_review, reason=reason
    )
