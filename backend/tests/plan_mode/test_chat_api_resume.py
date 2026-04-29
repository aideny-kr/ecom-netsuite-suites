"""Verify Plan Mode resume passes the ORIGINAL user query — not body.content — to the agent.

Codex P1: when the user clicks an option on a Plan Mode clarification card,
the frontend sends `body.content="Picked option A"` (or similar). The backend
already correctly REUSES the prior user message row (no duplicate row in DB).
But the user_message arg threaded into `_run_chat_background`/`_run_chat_pipeline`
must be the REUSED message's content (the original ambiguous query), not
`body.content`. Otherwise the agent ends up answering "Picked option A"
instead of the original question.

write_confirm short-circuits in the orchestrator before the agent runs, so
the user_message arg is moot for it — but for symmetry/defensive consistency
we apply the same fix uniformly: when reusing user_msg, also use its content.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from app.models.chat import ChatMessage, ChatSession


@pytest.mark.asyncio
async def test_plan_mode_resume_uses_original_query_in_background_path(client, db, admin_user):
    """Run-manager (background) path: user_message arg must be the ORIGINAL user query."""
    user, headers = admin_user
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))

    # Create a session with a prior ambiguous user message.
    session = ChatSession(tenant_id=user.tenant_id, user_id=user.id, title="Resume Test")
    db.add(session)
    await db.flush()

    original_query = "what's our revenue this quarter?"
    prior_user_msg = ChatMessage(
        tenant_id=user.tenant_id,
        session_id=session.id,
        role="user",
        content=original_query,
        created_at=datetime.now(timezone.utc) - timedelta(seconds=30),
    )
    db.add(prior_user_msg)
    # Add an assistant message with the clarification card (so the convo state is realistic).
    db.add(
        ChatMessage(
            tenant_id=user.tenant_id,
            session_id=session.id,
            role="assistant",
            content="",
            created_at=datetime.now(timezone.utc) - timedelta(seconds=15),
        )
    )
    await db.commit()

    # Mock RunManager so the background path is taken (rm.available=True).
    mock_rm = MagicMock()
    mock_rm.available = True
    mock_rm.get_active_run = MagicMock(return_value=None)
    mock_rm.create_run = MagicMock()
    mock_rm.write_event = MagicMock()
    mock_rm.set_status = MagicMock()
    mock_rm.clear_active_run = MagicMock()

    captured: dict = {}

    async def fake_run_chat_background(**kwargs):
        captured.update(kwargs)

    with (
        patch("app.api.v1.chat.get_run_manager", return_value=mock_rm),
        patch(
            "app.api.v1.chat._run_chat_background",
            side_effect=fake_run_chat_background,
        ),
    ):
        resp = await client.post(
            f"/api/v1/chat/sessions/{session.id}/messages",
            json={
                "content": "Picked option A",
                "plan_mode_choice": {
                    "action": "approve",
                    "confirmation_id": "msg-prior",
                    "option_id": "A",
                },
            },
            headers=headers,
        )

    # The run-manager path returns 202 Accepted with a run_id (background task spawned).
    assert resp.status_code == 202, resp.text
    body_json = resp.json()
    assert "run_id" in body_json
    # Allow the spawned task to actually run.
    import asyncio

    for _ in range(5):
        await asyncio.sleep(0)
        if captured:
            break

    assert captured, "expected _run_chat_background to have been called"
    assert captured["user_message"] == original_query, (
        f"Plan Mode resume must pass the ORIGINAL user query, got: {captured['user_message']!r}"
    )
    assert captured["user_message"] != "Picked option A"


@pytest.mark.asyncio
async def test_plan_mode_resume_uses_original_query_in_inline_sse_path(client, db, admin_user):
    """Inline-SSE fallback path (rm unavailable): user_message into run_chat_turn must also be original."""
    user, headers = admin_user
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))

    session = ChatSession(tenant_id=user.tenant_id, user_id=user.id, title="Inline SSE Resume Test")
    db.add(session)
    await db.flush()

    original_query = "show me revenue"
    db.add(
        ChatMessage(
            tenant_id=user.tenant_id,
            session_id=session.id,
            role="user",
            content=original_query,
            created_at=datetime.now(timezone.utc) - timedelta(seconds=30),
        )
    )
    await db.commit()

    captured: dict = {}

    async def mock_generator(**kwargs):
        captured.update(kwargs)
        yield {"type": "text", "content": "ok"}

    mock_rm = MagicMock()
    mock_rm.available = False  # forces inline SSE fallback

    with (
        patch("app.api.v1.chat.run_chat_turn", side_effect=mock_generator),
        patch("app.api.v1.chat.get_run_manager", return_value=mock_rm),
    ):
        resp = await client.post(
            f"/api/v1/chat/sessions/{session.id}/messages",
            json={
                "content": "Picked option B",
                "plan_mode_choice": {
                    "action": "approve",
                    "confirmation_id": "msg-prior",
                    "option_id": "B",
                },
            },
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    # Drain a chunk so the producer runs.
    async for _ in resp.aiter_lines():
        if captured:
            break

    assert captured, "expected run_chat_turn to have been called"
    assert captured["user_message"] == original_query, (
        f"Plan Mode resume (inline SSE) must pass the ORIGINAL user query, got: {captured['user_message']!r}"
    )
    assert captured["user_message"] != "Picked option B"
