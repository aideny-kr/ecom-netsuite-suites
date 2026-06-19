"""Regression: run_chat_turn must eager-load a freshly-created session's messages.

`ChatSession.messages` is `lazy="selectin"` — loaded when a session is fetched via a
query, but NOT on a constructed-then-flushed one. A caller that builds a fresh session
and ITERATES `run_chat_turn` (e.g. onboarding chat at api/v1/onboarding.py, integration
chat at api/v1/chat_integration.py) hands it a never-queried session; `run_chat_turn`'s
`session.messages` read would then emit an async lazy-load → `sqlalchemy.exc.MissingGreenlet`
→ 502.

`_ensure_session_messages_loaded` makes `run_chat_turn` self-sufficient (called before the
history reads), so any iterating caller is safe regardless of how the session was obtained.
"""

from __future__ import annotations

import contextlib
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import inspect as sa_inspect

from app.models.chat import ChatSession
from app.services.chat.orchestrator import _ensure_session_messages_loaded


def _mock_llm_patches():
    """Context managers that let real run_chat_turn produce a canned greeting with no LLM."""
    mock_response = AsyncMock()
    mock_response.tool_use_blocks = []
    mock_response.text_blocks = ["Welcome!"]
    mock_response.usage = AsyncMock(input_tokens=10, output_tokens=20)

    async def _stream(**kwargs):
        for text in mock_response.text_blocks:
            yield "text", text
        yield "response", mock_response

    mock_adapter = AsyncMock()
    mock_adapter.create_message = AsyncMock(return_value=mock_response)
    mock_adapter.stream_message = _stream

    return [
        patch("app.services.chat.orchestrator.get_adapter", return_value=mock_adapter),
        patch(
            "app.services.chat.orchestrator.get_tenant_ai_config",
            return_value=("anthropic", "claude-sonnet-4-5-20250929", "fake-key", False),
        ),
        patch("app.services.chat.orchestrator.deduct_chat_credits", new_callable=AsyncMock, return_value=None),
    ]


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


@pytest.mark.asyncio
async def test_run_chat_turn_does_not_greenlet_on_fresh_session(db, tenant_a):
    """Integration: real run_chat_turn must not MissingGreenlet on a freshly-created
    (never query-loaded) session — the helper loads `messages` before the read.
    Reproduces the onboarding/integration 502 without the fix (the existing onboarding
    tests dodge it by re-fetching with selectinload; this one deliberately does not).
    """
    user_id = uuid.uuid4()
    session = ChatSession(tenant_id=tenant_a.id, user_id=user_id, title="Onboarding", session_type="onboarding")
    db.add(session)
    await db.flush()  # FRESH session — deliberately NOT re-fetched with selectinload

    from app.services.chat.orchestrator import run_chat_turn

    events = []
    with contextlib.ExitStack() as stack:
        for p in _mock_llm_patches():
            stack.enter_context(p)
        async for ev in run_chat_turn(
            db=db, session=session, user_message="Hello", user_id=user_id, tenant_id=tenant_a.id
        ):
            events.append(ev)

    assert any(e.get("type") == "message" for e in events)


@pytest.mark.asyncio
async def test_integration_chat_returns_message_on_fresh_session(db, tenant_a):
    """integration_chat must ITERATE run_chat_turn (not await it) and return the assistant
    message on a fresh session — the endpoint 502'd on every request before this fix.
    """
    from app.api.v1.chat_integration import IntegrationChatRequest, integration_chat
    from app.core.api_key_auth import ApiKeyContext

    ctx = ApiKeyContext(tenant_id=tenant_a.id, scopes=["chat"])
    body = IntegrationChatRequest(message="Hello", session_id=None)

    with contextlib.ExitStack() as stack:
        for p in _mock_llm_patches():
            stack.enter_context(p)
        resp = await integration_chat(body=body, ctx=ctx, db=db)

    # The endpoint returns an assistant message instead of 502ing (await→iterate +
    # user_id fix). Exact content isn't asserted — a "Hello" hits the canned chitchat
    # path rather than the mocked LLM text.
    assert resp.role == "assistant"
    assert resp.content
    assert resp.session_id
