"""Detect knowledge gaps from failed queries and negative feedback."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.chat import ChatMessage, DocChunk

logger = structlog.get_logger()

APOLOGY_MARKERS = [
    "i wasn't able to",
    "i don't have information",
    "i'm not sure",
    "i could not find",
    "unable to determine",
    "i apologize",
    "unfortunately",
]

NETSUITE_RECORD_TYPES = [
    "rma",
    "return authorization",
    "rtnauth",
    "item receipt",
    "itemrcpt",
    "item fulfillment",
    "itemship",
    "purchase order",
    "purchord",
    "vendor bill",
    "vendbill",
    "sales order",
    "salesord",
    "invoice",
    "custinvc",
    "credit memo",
    "custcred",
    "customer payment",
    "custpymt",
    "transfer order",
    "trnfrord",
    "work order",
    "workord",
    "assembly build",
    "journal entry",
    "deposit",
    "estimate",
    "opportunity",
    "vendor credit",
    "inventory adjustment",
    "inventory transfer",
    "bin transfer",
    "custom record",
]


@dataclass
class KnowledgeGap:
    topic: str
    record_types: list[str] = field(default_factory=list)
    failed_queries: list[str] = field(default_factory=list)
    gap_score: float = 0.0
    message_count: int = 0


def _extract_record_types(text: str) -> list[str]:
    """Extract NetSuite record type mentions from text."""
    text_lower = text.lower()
    found = []
    for rt in NETSUITE_RECORD_TYPES:
        if rt in text_lower:
            found.append(rt)
    return list(set(found))


def _extract_topic(text: str) -> str:
    """Extract a topic slug from a question."""
    # Remove common question words
    text = re.sub(r"(?i)^(can you|please|show me|get|find|pull|what|how|list)\s+", "", text)
    # Take first 60 chars as topic
    return text[:60].strip().lower().replace(" ", "_")


async def _preceding_questions(db: AsyncSession, messages: list[ChatMessage]) -> dict:
    """Each assistant message's preceding user question in its session, in ONE query
    (it was one query per message): message id -> question text."""
    if not messages:
        return {}
    answer, question = aliased(ChatMessage), aliased(ChatMessage)
    latest = (
        select(question.content)
        .where(
            question.session_id == answer.session_id,
            question.role == "user",
            question.created_at < answer.created_at,
        )
        .order_by(question.created_at.desc())
        .limit(1)
        .lateral()
    )
    rows = await db.execute(
        select(answer.id, latest.c.content)
        .select_from(answer)
        .join(latest, true())
        .where(answer.id.in_([m.id for m in messages]))
    )
    return {row[0]: row[1] for row in rows.all()}


def _add_signal(gaps: dict, question: str, answer: str, weight: float) -> None:
    topic = _extract_topic(question)
    if topic not in gaps:
        gaps[topic] = KnowledgeGap(topic=topic)
    gaps[topic].record_types = (gaps[topic].record_types + _extract_record_types(question + " " + answer))[:10]
    if len(gaps[topic].failed_queries) < 5:
        gaps[topic].failed_queries.append(question[:200])
    gaps[topic].gap_score += weight
    gaps[topic].message_count += 1


async def detect_knowledge_gaps(
    db: AsyncSession,
    since_hours: int = 24,
    max_gaps: int = 5,
) -> list[KnowledgeGap]:
    """Identify topics where the agent struggled.

    A fixed number of queries whatever the volume: each signal's messages, their
    preceding questions in one query, and one doc_chunks scan for every gap's coverage.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    gaps: dict[str, KnowledgeGap] = {}

    # Signal 1: Thumbs-down votes
    thumbs_down = await db.execute(
        select(ChatMessage)
        .where(
            ChatMessage.user_feedback == "not_helpful",
            ChatMessage.created_at >= since,
            ChatMessage.role == "assistant",
        )
        .order_by(ChatMessage.created_at.desc())
        .limit(50)
    )
    voted = list(thumbs_down.scalars())
    questions = await _preceding_questions(db, voted)
    for msg in voted:
        question = questions.get(msg.id)
        if question:
            _add_signal(gaps, question, msg.content, 2.0)  # thumbs-down is strong signal

    # Signal 2: Tool errors with apology text
    error_msgs = await db.execute(
        select(ChatMessage)
        .where(
            ChatMessage.created_at >= since,
            ChatMessage.role == "assistant",
            or_(*[ChatMessage.content.ilike(f"%{marker}%") for marker in APOLOGY_MARKERS]),
        )
        .order_by(ChatMessage.created_at.desc())
        .limit(50)
    )

    failed = [
        msg
        for msg in error_msgs.scalars()
        # Only answers whose tool calls actually errored
        if any(
            isinstance(tc.get("result"), dict) and tc.get("result", {}).get("error")
            for tc in (msg.tool_calls or [])
            if isinstance(tc, dict)
        )
    ]
    questions = await _preceding_questions(db, failed)
    for msg in failed:
        question = questions.get(msg.id)
        if question:
            _add_signal(gaps, question, msg.content, 1.0)  # tool error is moderate signal

    # Check RAG coverage for every candidate in ONE scan of doc_chunks (it was one per gap).
    candidates = sorted(gaps.values(), key=lambda g: g.gap_score, reverse=True)[: max_gaps * 2]
    if not candidates:
        return []
    patterns = ["%" + gap.topic[:30].replace("%", "\\%").replace("_", "\\_") + "%" for gap in candidates]
    counts = (
        await db.execute(select(*[func.count(DocChunk.id).filter(DocChunk.content.ilike(p)) for p in patterns]))
    ).one()
    filtered_gaps = []
    for gap, chunk_count in zip(candidates, counts, strict=True):
        if (chunk_count or 0) < 2:
            gap.record_types = list(set(gap.record_types))
            filtered_gaps.append(gap)

    return filtered_gaps[:max_gaps]
