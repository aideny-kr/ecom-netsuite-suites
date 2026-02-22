"""Tests for chat API endpoints."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_create_session(client, db, admin_user):
    """POST /api/v1/chat/sessions → 201."""
    user, headers = admin_user
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))

    resp = await client.post("/api/v1/chat/sessions", json={"title": "Test Chat"}, headers=headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Test Chat"
    assert "id" in data
    assert data["is_archived"] is False


@pytest.mark.asyncio
async def test_create_session_no_title(client, db, admin_user):
    """POST /api/v1/chat/sessions with no title → 201."""
    user, headers = admin_user
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))

    resp = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] is None


@pytest.mark.asyncio
async def test_list_sessions(client, db, admin_user):
    """GET /api/v1/chat/sessions → 200."""
    user, headers = admin_user
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))

    # Create a session first
    await client.post("/api/v1/chat/sessions", json={"title": "Session 1"}, headers=headers)
    await client.post("/api/v1/chat/sessions", json={"title": "Session 2"}, headers=headers)

    resp = await client.get("/api/v1/chat/sessions", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 2


@pytest.mark.asyncio
async def test_get_session_detail(client, db, admin_user):
    """GET /api/v1/chat/sessions/{id} → 200 with messages."""
    user, headers = admin_user
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))

    create_resp = await client.post("/api/v1/chat/sessions", json={"title": "Detail Test"}, headers=headers)
    session_id = create_resp.json()["id"]

    resp = await client.get(f"/api/v1/chat/sessions/{session_id}", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == session_id
    assert data["title"] == "Detail Test"
    assert "messages" in data
    assert isinstance(data["messages"], list)


@pytest.mark.asyncio
async def test_get_session_not_found(client, db, admin_user):
    """GET /api/v1/chat/sessions/{random_id} → 404."""
    _, headers = admin_user
    random_id = str(uuid.uuid4())
    resp = await client.get(f"/api/v1/chat/sessions/{random_id}", headers=headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_tenant_isolation(client, db, admin_user, admin_user_b):
    """Cross-tenant session access → 404."""
    user_a, headers_a = admin_user
    _, headers_b = admin_user_b

    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user_a.tenant_id}'"))
    create_resp = await client.post("/api/v1/chat/sessions", json={"title": "Tenant A Session"}, headers=headers_a)
    session_id = create_resp.json()["id"]

    # User B should not see User A's session
    resp = await client.get(f"/api/v1/chat/sessions/{session_id}", headers=headers_b)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unauthenticated(client):
    """Unauthenticated requests → 401/403."""
    resp = await client.get("/api/v1/chat/sessions")
    assert resp.status_code in (401, 403)

    resp = await client.post("/api/v1/chat/sessions", json={})
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_send_message(client, db, admin_user):
    """POST /api/v1/chat/sessions/{id}/messages → 201 with mocked orchestrator."""
    user, headers = admin_user
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))

    create_resp = await client.post("/api/v1/chat/sessions", json={"title": "Msg Test"}, headers=headers)
    session_id = create_resp.json()["id"]

    # Mock the orchestrator as an async generator (SSE streaming)
    msg_dict = {
        "id": str(uuid.uuid4()),
        "role": "assistant",
        "content": "Hello! Here is your answer.",
        "tool_calls": None,
        "citations": None,
    }

    async def mock_generator(**kwargs):
        yield {"type": "text", "content": "Hello! Here is your answer."}
        yield {"type": "message", "message": msg_dict}

    with patch("app.api.v1.chat.run_chat_turn", side_effect=mock_generator):
        resp = await client.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            json={"content": "What are my recent orders?"},
            headers=headers,
        )
    # SSE streaming returns 200, not 201
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
