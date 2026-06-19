"""Regression: run_chat_turn must eager-load a freshly-created session's messages.

`ChatSession.messages` is `lazy="selectin"` — loaded when a session is fetched via a
query, but NOT on a constructed-then-flushed one. Callers that build a fresh session
(onboarding chat at api/v1/onboarding.py, integration chat at api/v1/chat_integration.py)
hand it straight to `run_chat_turn`, which reads `session.messages`. On the never-queried
session that read emits an async lazy-load → `sqlalchemy.exc.MissingGreenlet` → 502.

`_ensure_session_messages_loaded` makes `run_chat_turn` self-sufficient (called before the
history reads), so any caller is safe regardless of how the session was obtained.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import inspect as sa_inspect

from app.models.chat import ChatSession
from app.services.chat.orchestrator import _ensure_session_messages_loaded


@pytest.mark.asyncio
async def test_ensure_messages_loaded_on_fresh_session(db, tenant_a):
    # A freshly-created session — exactly how onboarding/integration chat build one.
    session = ChatSession(
        tenant_id=tenant_a.id,
        user_id=uuid.uuid4(),
        title="Onboarding",
        session_type="onboarding",
    )
    db.add(session)
    await db.flush()

    # Precondition = the bug shape: messages is NOT loaded on a never-queried session,
    # so reading it (as run_chat_turn does) would trigger an async lazy-load.
    assert "messages" in sa_inspect(session).unloaded

    # The fix loads it without an implicit async lazy-load (no MissingGreenlet).
    await _ensure_session_messages_loaded(db, session)

    assert "messages" not in sa_inspect(session).unloaded
    assert session.messages == []


@pytest.mark.asyncio
async def test_ensure_messages_loaded_is_noop_when_already_loaded(db, tenant_a):
    session = ChatSession(
        tenant_id=tenant_a.id,
        user_id=uuid.uuid4(),
        session_type="chat",
    )
    db.add(session)
    await db.flush()

    await _ensure_session_messages_loaded(db, session)  # first call loads it
    assert "messages" not in sa_inspect(session).unloaded

    # Idempotent: a second call on an already-loaded session does not raise.
    await _ensure_session_messages_loaded(db, session)
    assert session.messages == []
