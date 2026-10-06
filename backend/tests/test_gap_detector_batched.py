"""Knowledge-gap detection runs a fixed number of queries (N+1 fix, 2026-10-05).

Before: one 'previous user question' query per thumbs-down answer and per failed answer
(up to 50 + 50), plus one full ILIKE scan of doc_chunks per candidate gap (up to 10).
After: one query per signal for the questions and one scan for all coverage counts. The
pre-fix algorithm is kept here as a reference; both must return the same gaps.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, or_, select

from app.models.chat import ChatMessage, ChatSession, DocChunk
from app.services.knowledge import gap_detector
from app.services.knowledge.gap_detector import (
    APOLOGY_MARKERS,
    KnowledgeGap,
    _extract_record_types,
    _extract_topic,
    detect_knowledge_gaps,
)


async def _reference(db, since_hours=24, max_gaps=5):
    """The pre-fix detect_knowledge_gaps, verbatim apart from its name."""
    since = datetime.now(UTC) - timedelta(hours=since_hours)
    gaps: dict[str, KnowledgeGap] = {}
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
    for msg in thumbs_down.scalars().all():
        user_question = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.session_id == msg.session_id,
                    ChatMessage.role == "user",
                    ChatMessage.created_at < msg.created_at,
                )
                .order_by(ChatMessage.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if not user_question:
            continue
        topic = _extract_topic(user_question.content)
        record_types = _extract_record_types(user_question.content + " " + msg.content)
        gaps.setdefault(topic, KnowledgeGap(topic=topic))
        gaps[topic].record_types = (gaps[topic].record_types + record_types)[:10]
        if len(gaps[topic].failed_queries) < 5:
            gaps[topic].failed_queries.append(user_question.content[:200])
        gaps[topic].gap_score += 2.0
        gaps[topic].message_count += 1
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
    for msg in error_msgs.scalars().all():
        tool_calls = msg.tool_calls or []
        if not any(
            isinstance(tc.get("result"), dict) and tc.get("result", {}).get("error")
            for tc in tool_calls
            if isinstance(tc, dict)
        ):
            continue
        user_question = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.session_id == msg.session_id,
                    ChatMessage.role == "user",
                    ChatMessage.created_at < msg.created_at,
                )
                .order_by(ChatMessage.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if not user_question:
            continue
        topic = _extract_topic(user_question.content)
        record_types = _extract_record_types(user_question.content + " " + msg.content)
        gaps.setdefault(topic, KnowledgeGap(topic=topic))
        gaps[topic].record_types = (gaps[topic].record_types + record_types)[:10]
        if len(gaps[topic].failed_queries) < 5:
            gaps[topic].failed_queries.append(user_question.content[:200])
        gaps[topic].gap_score += 1.0
        gaps[topic].message_count += 1
    filtered = []
    for gap in sorted(gaps.values(), key=lambda g: g.gap_score, reverse=True)[: max_gaps * 2]:
        topic_escaped = gap.topic[:30].replace("%", "\\%").replace("_", "\\_")
        count = (
            await db.execute(select(func.count(DocChunk.id)).where(DocChunk.content.ilike(f"%{topic_escaped}%")))
        ).scalar() or 0
        if count < 2:
            gap.record_types = list(set(gap.record_types))
            filtered.append(gap)
    return filtered[:max_gaps]


async def _seed(db):
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    error_calls = [{"tool": "netsuite_suiteql", "result": {"error": "Invalid column"}}]
    conversations = [
        # (question, answer, feedback, tool_calls) -- each in its own session
        ("show me RMAs pending receipt", "Here they are", "not_helpful", None),
        ("list item receipts for PO 123", "I wasn't able to find that", None, error_calls),
        ("show me RMAs pending receipt", "Unfortunately the query failed", "not_helpful", error_calls),
        ("vendor bills over 10k", "I could not find vendor bills", None, error_calls),
        ("vendor bills over 10k", "Fine answer", None, None),
        ("covered topic about deposits", "Unfortunately it failed", None, error_calls),
    ]
    for i, (question, answer, feedback, calls) in enumerate(conversations):
        session = ChatSession(tenant_id=tenant_id, user_id=user_id, title=f"gap test {i}")
        db.add(session)
        await db.flush()
        asked = now - timedelta(minutes=60 - i * 5)
        db.add(
            ChatMessage(
                tenant_id=tenant_id,
                session_id=session.id,
                role="user",
                content="earlier question",
                created_at=asked - timedelta(minutes=2),
            )
        )
        db.add(ChatMessage(tenant_id=tenant_id, session_id=session.id, role="user", content=question, created_at=asked))
        db.add(
            ChatMessage(
                tenant_id=tenant_id,
                session_id=session.id,
                role="assistant",
                content=answer,
                user_feedback=feedback,
                tool_calls=calls,
                created_at=asked + timedelta(seconds=30),
            )
        )
    # Two chunks cover the "deposits" topic, so that gap is filtered out as already covered.
    for j in range(2):
        db.add(
            DocChunk(
                tenant_id=tenant_id,
                source_path="kb/deposits.md",
                title="Deposits",
                chunk_index=j,
                content="Notes on covered_topic_about_deposits and more",
                token_count=10,
            )
        )
    await db.flush()


@pytest.mark.asyncio
async def test_batched_detection_returns_what_the_per_row_queries_returned(db):
    await _seed(db)
    expected = await _reference(db)
    got = await detect_knowledge_gaps(db)
    key = lambda gaps: [(g.topic, sorted(g.record_types), g.failed_queries, g.gap_score, g.message_count) for g in gaps]  # noqa: E731
    assert key(got) == key(expected)
    topics = [g.topic for g in got]
    assert "rmas_pending_receipt" in " ".join(topics) and "covered_topic_about_deposits" not in topics


@pytest.mark.asyncio
async def test_a_fixed_number_of_queries_however_many_messages(db, monkeypatch):
    await _seed(db)
    calls = []
    original = db.execute

    async def counting_execute(*args, **kwargs):
        calls.append(args[0])
        return await original(*args, **kwargs)

    monkeypatch.setattr(db, "execute", counting_execute)
    await detect_knowledge_gaps(db)
    # thumbs-down list, their questions, error list, their questions, one coverage scan.
    assert len(calls) <= 5, len(calls)
    assert gap_detector  # module imported for the record
