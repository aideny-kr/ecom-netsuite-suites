"""Eval case miner — mines successful user queries from chat history into organic eval cases.

Runs as part of the nightly autonomous query improvement loop to grow the eval case pool
from real user queries rather than relying solely on static YAML.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import anthropic
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.chat import ChatMessage
from app.models.eval_case import EvalCase as EvalCaseModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_MIN_CONFIDENCE = 4.0
_MAX_CASES_PER_RUN = 10
_MIN_QUESTION_LENGTH = 10
_DUPLICATE_OVERLAP_THRESHOLD = 0.80
_MAX_KEYWORDS = 8

_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will", "would",
        "could", "should", "may", "might", "can", "shall", "not", "no", "nor",
        "so", "yet", "both", "either", "neither", "each", "every", "all",
        "any", "few", "more", "most", "other", "some", "such", "what", "which",
        "who", "whom", "how", "when", "where", "why", "me", "my", "our",
        "your", "his", "her", "its", "their", "we", "you", "he", "she", "it",
        "they", "i", "this", "that", "these", "those", "as", "if", "then",
        "than", "because", "while", "although", "though", "since", "until",
        "unless", "after", "before", "during", "about", "above", "across",
        "against", "along", "among", "around", "between", "beyond", "despite",
        "except", "into", "like", "near", "off", "out", "over", "past",
        "regarding", "through", "throughout", "under", "upon", "within",
        "without", "up", "down", "get", "show", "give", "list", "tell",
        "find", "me", "us",
    }
)

_KEYWORD_EXTRACTION_SYSTEM = (
    "You are a test case designer. Given a user question and the data result it produced, "
    "extract 3-8 keywords that MUST appear in a correct answer. Include domain terms, "
    "column names from results, and expected value patterns. "
    "Return ONLY a JSON array of lowercase strings."
)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _detect_dialect(tool_name: str) -> str | None:
    """Return 'suiteql', 'bigquery', or None based on tool name."""
    normalized = tool_name.lower().strip()
    if normalized in {"netsuite_suiteql", "netsuite.suiteql"}:
        return "suiteql"
    if normalized in {"bigquery_sql", "bigquery.sql"}:
        return "bigquery"
    return None


def _tokenize(text: str) -> set[str]:
    """Split text into lowercase words, removing stopwords."""
    words = text.lower().split()
    return {w.strip(".,!?;:\"'()[]{}") for w in words if w.strip(".,!?;:\"'()[]{}") not in _STOPWORDS}


def _is_duplicate(question: str, existing_questions: list[str]) -> bool:
    """Return True if question overlaps >= 80% with any existing question (word overlap ratio)."""
    q_tokens = _tokenize(question)
    if not q_tokens:
        return False
    for existing in existing_questions:
        e_tokens = _tokenize(existing)
        if not e_tokens:
            continue
        intersection = q_tokens & e_tokens
        union = q_tokens | e_tokens
        if not union:
            continue
        overlap = len(intersection) / len(union)
        if overlap >= _DUPLICATE_OVERLAP_THRESHOLD:
            return True
    return False


def _fallback_keywords(question: str) -> list[str]:
    """Extract keywords from question by removing stopwords, returning words > 2 chars, max 8."""
    words = question.lower().split()
    keywords = [
        w.strip(".,!?;:\"'()[]{}") for w in words
        if len(w.strip(".,!?;:\"'()[]{}")) > 2 and w.strip(".,!?;:\"'()[]{}") not in _STOPWORDS
    ]
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for kw in keywords:
        if kw and kw not in seen:
            seen.add(kw)
            unique.append(kw)
    return unique[:_MAX_KEYWORDS]


async def _call_haiku(system: str, user_msg: str) -> str:
    """Call Claude Haiku with a system + user message, return response text."""
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    response = await client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return response.content[0].text.strip()


async def _extract_keywords_via_haiku(question: str, result_text: str) -> list[str]:
    """Use Haiku to extract 3-8 keywords that must appear in a correct answer.

    Falls back to _fallback_keywords() on any error (timeout, parse failure).
    """
    try:
        user_msg = f"Question: {question}\n\nResult data: {result_text[:500]}"
        raw = await _call_haiku(_KEYWORD_EXTRACTION_SYSTEM, user_msg)
        # Strip markdown code fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1]) if len(lines) > 2 else raw
        keywords = json.loads(raw)
        if not isinstance(keywords, list):
            raise ValueError("Response is not a list")
        cleaned = [str(k).lower().strip() for k in keywords if k]
        return cleaned[:_MAX_KEYWORDS] if len(cleaned) >= 3 else _fallback_keywords(question)
    except Exception as exc:
        logger.warning("eval_case_miner: keyword extraction failed (%s), using fallback", exc)
        return _fallback_keywords(question)


def _get_result_data(tool_call: dict) -> dict:
    """Extract result data dict from a tool call — checks result_payload first, then result."""
    payload = tool_call.get("result_payload")
    if isinstance(payload, dict):
        return payload
    result = tool_call.get("result")
    if isinstance(result, dict):
        return result
    return {}


def _tool_call_succeeded(tool_call: dict) -> bool:
    """Return True if the tool call has no error and has actual data (columns/rows/items)."""
    data = _get_result_data(tool_call)
    # Check for error
    error = data.get("error")
    if error is True or (isinstance(error, str) and error.strip()):
        return False
    # Must have data fields
    return bool(data.get("columns") or data.get("rows") or data.get("items"))


# ---------------------------------------------------------------------------
# Core mining logic
# ---------------------------------------------------------------------------


async def mine_organic_eval_cases(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    lookback_hours: int = 24,
) -> list[dict]:
    """Mine successful assistant messages to build organic eval cases.

    Returns a list of dicts ready to be stored via store_mined_cases().
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    # 1. Fetch qualifying assistant messages (high confidence, has tool calls, recent)
    stmt = select(ChatMessage).where(
        and_(
            ChatMessage.tenant_id == tenant_id,
            ChatMessage.role == "assistant",
            ChatMessage.confidence_score >= _MIN_CONFIDENCE,
            ChatMessage.created_at >= cutoff,
            ChatMessage.tool_calls.is_not(None),
        )
    ).order_by(ChatMessage.created_at.desc())

    result = await db.execute(stmt)
    assistant_messages: list[ChatMessage] = list(result.scalars().all())

    if not assistant_messages:
        logger.info("eval_case_miner: no qualifying assistant messages for tenant %s", tenant_id)
        return []

    # 2. Fetch existing eval case questions to dedup against
    existing_stmt = select(EvalCaseModel.question).where(
        and_(EvalCaseModel.tenant_id == tenant_id, EvalCaseModel.is_active.is_(True))
    )
    existing_result = await db.execute(existing_stmt)
    existing_questions: list[str] = [row[0] for row in existing_result.fetchall()]

    cases: list[dict] = []

    for msg in assistant_messages:
        if len(cases) >= _MAX_CASES_PER_RUN:
            break

        tool_calls = msg.tool_calls
        if not isinstance(tool_calls, list):
            continue

        # 3. Find first successful data tool call
        matched_tc: dict | None = None
        dialect: str | None = None
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            tool_name = tc.get("tool", "")
            d = _detect_dialect(tool_name)
            if d is None:
                continue
            if _tool_call_succeeded(tc):
                matched_tc = tc
                dialect = d
                break

        if matched_tc is None or dialect is None:
            continue

        # 4. Find the preceding user message in the same session
        user_stmt = select(ChatMessage).where(
            and_(
                ChatMessage.session_id == msg.session_id,
                ChatMessage.role == "user",
                ChatMessage.created_at < msg.created_at,
            )
        ).order_by(ChatMessage.created_at.desc()).limit(1)
        user_result = await db.execute(user_stmt)
        user_msg = user_result.scalar_one_or_none()

        if user_msg is None:
            continue

        question = (user_msg.content or "").strip()
        if len(question) < _MIN_QUESTION_LENGTH:
            continue

        # 5. Deduplicate
        if _is_duplicate(question, existing_questions):
            logger.debug("eval_case_miner: skipping duplicate question: %s", question[:60])
            continue

        # 6. Extract SQL from matched tool call
        generated_sql = (matched_tc.get("params") or {}).get("query", "")

        # 7. Extract keywords via Haiku
        result_data = _get_result_data(matched_tc)
        result_text = json.dumps(result_data)[:500]
        keywords = await _extract_keywords_via_haiku(question, result_text)

        case: dict[str, Any] = {
            "question": question,
            "dialect": dialect,
            "expected_keywords": keywords,
            "source_message_id": msg.id,
            "generated_sql": generated_sql,
            "confidence_score": float(msg.confidence_score) if msg.confidence_score is not None else None,
        }
        cases.append(case)
        # Add to existing_questions to dedup within this run
        existing_questions.append(question)

    logger.info(
        "eval_case_miner: mined %d cases for tenant %s (lookback=%dh)",
        len(cases),
        tenant_id,
        lookback_hours,
    )
    return cases


async def store_mined_cases(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    cases: list[dict],
) -> int:
    """Persist mined eval cases to the database with source='organic'.

    Returns the count of cases stored.
    """
    stored = 0
    for case in cases:
        eval_case = EvalCaseModel(
            tenant_id=tenant_id,
            question=case["question"],
            dialect=case["dialect"],
            expected_keywords=case.get("expected_keywords", []),
            source="organic",
            source_message_id=case.get("source_message_id"),
            generated_sql=case.get("generated_sql"),
            confidence_score=case.get("confidence_score"),
        )
        db.add(eval_case)
        stored += 1

    if stored:
        await db.flush()

    logger.info("eval_case_miner: stored %d organic eval cases for tenant %s", stored, tenant_id)
    return stored
