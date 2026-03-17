"""Detect knowledge gaps from failed queries and negative feedback."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

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
    "rma", "return authorization", "rtnauth",
    "item receipt", "itemrcpt", "item fulfillment", "itemship",
    "purchase order", "purchord", "vendor bill", "vendbill",
    "sales order", "salesord", "invoice", "custinvc",
    "credit memo", "custcred", "customer payment", "custpymt",
    "transfer order", "trnfrord", "work order", "workord",
    "assembly build", "journal entry", "deposit", "estimate",
    "opportunity", "vendor credit", "inventory adjustment",
    "inventory transfer", "bin transfer", "custom record",
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


async def detect_knowledge_gaps(
    db: AsyncSession,
    since_hours: int = 24,
    max_gaps: int = 5,
) -> list[KnowledgeGap]:
    """Identify topics where the agent struggled."""
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

    for msg in thumbs_down.scalars():
        # Find the user's original question (previous message in session)
        user_msg = await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.session_id == msg.session_id,
                ChatMessage.role == "user",
                ChatMessage.created_at < msg.created_at,
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(1)
        )
        user_question = user_msg.scalar_one_or_none()
        if not user_question:
            continue

        topic = _extract_topic(user_question.content)
        record_types = _extract_record_types(user_question.content + " " + msg.content)

        if topic not in gaps:
            gaps[topic] = KnowledgeGap(topic=topic)
        gaps[topic].record_types.extend(record_types)
        gaps[topic].failed_queries.append(user_question.content[:200])
        gaps[topic].gap_score += 2.0  # thumbs-down is strong signal
        gaps[topic].message_count += 1

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

    for msg in error_msgs.scalars():
        # Check if tool calls had errors
        tool_calls = msg.tool_calls or []
        has_error = any(
            isinstance(tc.get("result"), dict) and tc.get("result", {}).get("error")
            for tc in tool_calls
            if isinstance(tc, dict)
        )
        if not has_error:
            continue

        user_msg = await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.session_id == msg.session_id,
                ChatMessage.role == "user",
                ChatMessage.created_at < msg.created_at,
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(1)
        )
        user_question = user_msg.scalar_one_or_none()
        if not user_question:
            continue

        topic = _extract_topic(user_question.content)
        record_types = _extract_record_types(user_question.content + " " + msg.content)

        if topic not in gaps:
            gaps[topic] = KnowledgeGap(topic=topic)
        gaps[topic].record_types.extend(record_types)
        gaps[topic].failed_queries.append(user_question.content[:200])
        gaps[topic].gap_score += 1.0  # tool error is moderate signal
        gaps[topic].message_count += 1

    # Check RAG coverage for each gap
    filtered_gaps = []
    for gap in sorted(gaps.values(), key=lambda g: g.gap_score, reverse=True)[:max_gaps * 2]:
        # Search for existing coverage
        coverage = await db.execute(
            select(func.count(DocChunk.id))
            .where(DocChunk.content.ilike(f"%{gap.topic[:30]}%"))
        )
        chunk_count = coverage.scalar() or 0
        if chunk_count < 2:
            gap.record_types = list(set(gap.record_types))
            filtered_gaps.append(gap)

    return filtered_gaps[:max_gaps]
