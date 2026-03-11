"""Tests for user feedback loop — Cycles 1-5."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatMessage, ChatSession
from app.models.tenant_query_pattern import TenantQueryPattern
from app.services.query_pattern_service import process_feedback

SESSION_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_session(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> ChatSession:
    session = ChatSession(
        tenant_id=tenant_id,
        user_id=user_id,
        title="Test",
    )
    db.add(session)
    await db.flush()
    return session


async def _create_assistant_msg(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    content: str = "Total orders: 42",
    tool_calls: list | None = None,
) -> ChatMessage:
    msg = ChatMessage(
        tenant_id=tenant_id,
        session_id=session_id,
        role="assistant",
        content=content,
        tool_calls=tool_calls,
    )
    db.add(msg)
    await db.flush()
    return msg


# ---------------------------------------------------------------------------
# Cycle 1 — Model column
# ---------------------------------------------------------------------------


class TestChatMessageFeedbackColumn:
    def test_chat_message_has_user_feedback(self):
        msg = ChatMessage(
            tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            session_id=SESSION_ID,
            role="assistant",
            content="test",
            user_feedback="helpful",
        )
        assert msg.user_feedback == "helpful"

    def test_chat_message_feedback_defaults_none(self):
        msg = ChatMessage(
            tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            session_id=SESSION_ID,
            role="assistant",
            content="test",
        )
        assert msg.user_feedback is None


# ---------------------------------------------------------------------------
# Cycle 2 — API endpoint
# ---------------------------------------------------------------------------


class TestFeedbackEndpoint:
    @pytest.mark.asyncio
    async def test_set_feedback_helpful(self, client, admin_user, db):
        user, headers = admin_user
        session = await _create_session(db, user.tenant_id, user.id)
        msg = await _create_assistant_msg(
            db, user.tenant_id, session.id,
            tool_calls=[{"tool": "netsuite_suiteql", "params": {"query": "SELECT 1"}}],
        )
        await db.flush()

        resp = await client.patch(
            f"/api/v1/chat/messages/{msg.id}/feedback",
            params={"feedback": "helpful"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["feedback"] == "helpful"

    @pytest.mark.asyncio
    async def test_set_feedback_not_helpful(self, client, admin_user, db):
        user, headers = admin_user
        session = await _create_session(db, user.tenant_id, user.id)
        msg = await _create_assistant_msg(
            db, user.tenant_id, session.id,
            tool_calls=[{"tool": "netsuite_suiteql", "params": {"query": "SELECT 1"}}],
        )
        await db.flush()

        resp = await client.patch(
            f"/api/v1/chat/messages/{msg.id}/feedback",
            params={"feedback": "not_helpful"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["feedback"] == "not_helpful"

    @pytest.mark.asyncio
    async def test_set_feedback_invalid_value(self, client, admin_user, db):
        user, headers = admin_user
        session = await _create_session(db, user.tenant_id, user.id)
        msg = await _create_assistant_msg(db, user.tenant_id, session.id)
        await db.flush()

        resp = await client.patch(
            f"/api/v1/chat/messages/{msg.id}/feedback",
            params={"feedback": "maybe"},
            headers=headers,
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_set_feedback_nonexistent_message(self, client, admin_user):
        _, headers = admin_user
        fake_id = str(uuid.uuid4())
        resp = await client.patch(
            f"/api/v1/chat/messages/{fake_id}/feedback",
            params={"feedback": "helpful"},
            headers=headers,
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_set_feedback_wrong_tenant(self, client, admin_user, admin_user_b, db):
        """User from tenant B cannot set feedback on tenant A's message."""
        user_a, _ = admin_user
        _, headers_b = admin_user_b
        session = await _create_session(db, user_a.tenant_id, user_a.id)
        msg = await _create_assistant_msg(db, user_a.tenant_id, session.id)
        await db.flush()

        resp = await client.patch(
            f"/api/v1/chat/messages/{msg.id}/feedback",
            params={"feedback": "helpful"},
            headers=headers_b,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cycle 3 — Serialization
# ---------------------------------------------------------------------------


class TestFeedbackSerialization:
    @pytest.mark.asyncio
    async def test_serialize_message_includes_feedback(self, client, admin_user, db):
        user, headers = admin_user
        session = await _create_session(db, user.tenant_id, user.id)
        msg = await _create_assistant_msg(db, user.tenant_id, session.id)
        msg.user_feedback = "helpful"
        await db.flush()

        resp = await client.get(
            f"/api/v1/chat/sessions/{session.id}",
            headers=headers,
        )
        assert resp.status_code == 200
        messages = resp.json()["messages"]
        assert len(messages) == 1
        assert messages[0]["user_feedback"] == "helpful"

    @pytest.mark.asyncio
    async def test_serialize_message_null_feedback(self, client, admin_user, db):
        user, headers = admin_user
        session = await _create_session(db, user.tenant_id, user.id)
        await _create_assistant_msg(db, user.tenant_id, session.id)
        await db.flush()

        resp = await client.get(
            f"/api/v1/chat/sessions/{session.id}",
            headers=headers,
        )
        assert resp.status_code == 200
        messages = resp.json()["messages"]
        assert messages[0].get("user_feedback") is None


# ---------------------------------------------------------------------------
# Cycle 4 — Feedback processing (pattern service)
# ---------------------------------------------------------------------------


class TestProcessFeedback:
    @pytest.mark.asyncio
    async def test_helpful_feedback_increments_pattern(self, admin_user, db):
        user, _ = admin_user
        session = await _create_session(db, user.tenant_id, user.id)
        sql = "SELECT COUNT(*) as cnt FROM transaction WHERE type = 'SalesOrd'"

        pattern = TenantQueryPattern(
            tenant_id=user.tenant_id,
            user_question="how many sales orders",
            working_sql=sql,
            success_count=3,
        )
        db.add(pattern)
        await db.flush()

        msg = await _create_assistant_msg(
            db, user.tenant_id, session.id,
            content="There are 42 sales orders.",
            tool_calls=[{"tool": "netsuite_suiteql", "params": {"query": sql}}],
        )

        await process_feedback(db, user.tenant_id, msg, "helpful")
        await db.flush()

        await db.refresh(pattern)
        assert pattern.success_count == 4

    @pytest.mark.asyncio
    async def test_not_helpful_decrements_pattern(self, admin_user, db):
        user, _ = admin_user
        session = await _create_session(db, user.tenant_id, user.id)
        sql = "SELECT SUM(t.total) FROM transaction t WHERE type = 'PurchOrd'"

        pattern = TenantQueryPattern(
            tenant_id=user.tenant_id,
            user_question="total PO amount",
            working_sql=sql,
            success_count=3,
        )
        db.add(pattern)
        await db.flush()

        msg = await _create_assistant_msg(
            db, user.tenant_id, session.id,
            content="The total is -$500,000.",
            tool_calls=[{"tool": "netsuite_suiteql", "params": {"query": sql}}],
        )

        await process_feedback(db, user.tenant_id, msg, "not_helpful")
        await db.flush()

        await db.refresh(pattern)
        assert pattern.success_count == 2

    @pytest.mark.asyncio
    async def test_not_helpful_floors_at_zero(self, admin_user, db):
        user, _ = admin_user
        session = await _create_session(db, user.tenant_id, user.id)
        sql = "SELECT * FROM transaction WHERE 1=0"

        pattern = TenantQueryPattern(
            tenant_id=user.tenant_id,
            user_question="bad query",
            working_sql=sql,
            success_count=0,
        )
        db.add(pattern)
        await db.flush()

        msg = await _create_assistant_msg(
            db, user.tenant_id, session.id,
            content="No results.",
            tool_calls=[{"tool": "netsuite_suiteql", "params": {"query": sql}}],
        )

        await process_feedback(db, user.tenant_id, msg, "not_helpful")
        await db.flush()

        await db.refresh(pattern)
        assert pattern.success_count == 0

    @pytest.mark.asyncio
    async def test_no_matching_pattern_is_noop(self, admin_user, db):
        user, _ = admin_user
        session = await _create_session(db, user.tenant_id, user.id)

        msg = await _create_assistant_msg(
            db, user.tenant_id, session.id,
            content="Result: 1",
            tool_calls=[{"tool": "netsuite_suiteql", "params": {"query": "SELECT 1 FROM dual"}}],
        )

        # Should not raise
        await process_feedback(db, user.tenant_id, msg, "helpful")

    @pytest.mark.asyncio
    async def test_message_without_tool_calls_is_noop(self, admin_user, db):
        user, _ = admin_user
        session = await _create_session(db, user.tenant_id, user.id)

        msg = await _create_assistant_msg(
            db, user.tenant_id, session.id,
            content="Here is some documentation...",
            tool_calls=None,
        )

        # Should not raise
        await process_feedback(db, user.tenant_id, msg, "helpful")

    @pytest.mark.asyncio
    async def test_non_suiteql_tool_calls_ignored(self, admin_user, db):
        user, _ = admin_user
        session = await _create_session(db, user.tenant_id, user.id)

        msg = await _create_assistant_msg(
            db, user.tenant_id, session.id,
            content="Found docs.",
            tool_calls=[{"tool": "rag_search", "params": {"query": "netsuite"}}],
        )

        await process_feedback(db, user.tenant_id, msg, "helpful")
