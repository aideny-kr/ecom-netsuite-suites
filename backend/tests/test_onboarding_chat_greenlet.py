"""Regression: onboarding chat must eager-load the session's `messages` relationship.

`ChatSession.messages` is `lazy="selectin"` — auto-loaded when a session is fetched
via a query (normal chat does `select(ChatSession)`), but NOT on a freshly-created
one. `start_onboarding_chat` CREATES a new session and hands it to `run_chat_turn`,
which accesses `session.messages` (orchestrator.py `if session.messages:`). On the
never-queried session that access emits an async lazy-load → `sqlalchemy.exc.
MissingGreenlet` → the endpoint 502s ("Failed to start onboarding chat").

This test stubs `run_chat_turn` to touch `session.messages` the same way the real
orchestrator does, so it reproduces the failure and pins the fix (eager-load).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.api.v1.onboarding import start_onboarding_chat


@pytest.mark.asyncio
async def test_start_onboarding_chat_eager_loads_session_messages(db, admin_user, monkeypatch):
    user, _headers = admin_user  # fresh user in tenant_a, no existing onboarding session

    accessed: dict = {}

    async def fake_run_chat_turn(*, db, session, user_message, user_id, tenant_id, **kwargs):
        # Mirror orchestrator.py `if session.messages:` — must NOT raise
        # MissingGreenlet on the freshly-created onboarding session.
        accessed["messages"] = list(session.messages)
        yield {
            "type": "message",
            "message": {
                "id": str(uuid.uuid4()),
                "role": "assistant",
                "content": "Welcome! Let's set up your account.",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    monkeypatch.setattr("app.api.v1.onboarding.run_chat_turn", fake_run_chat_turn)

    resp = await start_onboarding_chat(user=user, db=db)

    # The orchestrator-style access saw an empty (eager-loaded) collection, no greenlet error.
    assert accessed.get("messages") == []
    assert resp.message["role"] == "assistant"
    assert resp.message["content"] == "Welcome! Let's set up your account."
