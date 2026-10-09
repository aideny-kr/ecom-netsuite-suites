"""Actual Redis + DB checks for private chat run stream/cancellation."""

import uuid
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.models.chat import ChatSession
from app.services.chat.run_manager import RunManager


@pytest.mark.asyncio
async def test_private_runs_require_current_session_owner(client, db, admin_user, member_user, admin_user_b):
    owner, headers = admin_user
    _, peer_headers = member_user
    _, foreign_headers = admin_user_b
    session = ChatSession(tenant_id=owner.tenant_id, user_id=owner.id, title="Synthetic private run")
    db.add(session)
    await db.flush()
    run_id = str(uuid.uuid4())
    manager = RunManager(redis_url=settings.REDIS_URL)
    assert manager.available
    manager.create_run(run_id, str(session.id))
    manager.write_event(run_id, {"type": "text", "content": "Synthetic owner-only evidence"})
    manager.set_status(run_id, "complete")
    try:
        with patch("app.api.v1.chat_runs.get_run_manager", return_value=manager):
            for denied in (peer_headers, foreign_headers):
                response = await client.get(f"/api/v1/chat/runs/{run_id}/stream", headers=denied)
                assert response.status_code == 404
                assert "owner-only" not in response.text
                manager.set_status(run_id, "running")
                assert (await client.post(f"/api/v1/chat/runs/{run_id}/cancel", headers=denied)).status_code == 404
                manager.set_status(run_id, "complete")
                assert not manager.is_cancelled(run_id)
            manager.set_status(run_id, "running")
            assert (await client.post(f"/api/v1/chat/runs/{run_id}/cancel", headers=headers)).status_code == 200
            assert manager.is_cancelled(run_id)
            manager.set_status(run_id, "complete")
            manager.clear_active_run(str(session.id))
            # Completion removes the active pointer but must retain ownership for replay.
            response = await client.get(f"/api/v1/chat/runs/{run_id}/stream", headers=headers)
            assert response.status_code == 200
            assert "Synthetic owner-only evidence" in response.text
            assert manager.get_session_id(run_id) == str(session.id)
            owner.is_active = False
            await db.flush()
            assert (await client.get(f"/api/v1/chat/runs/{run_id}/stream", headers=headers)).status_code in (401, 403)
            assert (await client.post(f"/api/v1/chat/runs/{run_id}/cancel", headers=headers)).status_code in (401, 403)
            owner.is_active = True
            await db.flush()
            # Losing the binding must fail closed, including legacy pre-upgrade runs.
            manager._redis.delete(f"chat:run:{run_id}:session")
            assert (await client.get(f"/api/v1/chat/runs/{run_id}/stream", headers=headers)).status_code == 404
    finally:
        manager.clear_active_run(str(session.id))
        for key in manager._redis.scan_iter(f"chat:run:{run_id}:*"):
            manager._redis.delete(key)
