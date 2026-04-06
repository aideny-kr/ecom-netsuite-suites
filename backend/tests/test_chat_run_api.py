"""Tests for chat run API endpoints (SSE relay + cancel)."""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import get_current_user
from app.main import app
from app.models.user import User

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TENANT_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()


def _fake_user() -> User:
    user = MagicMock(spec=User)
    user.id = _USER_ID
    user.tenant_id = _TENANT_ID
    user.email = "test@example.com"
    return user


@pytest.fixture()
def client():
    """TestClient with auth overridden."""
    fake_user = _fake_user()
    app.dependency_overrides[get_current_user] = lambda: fake_user
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def mock_rm():
    """Patch get_run_manager and return the mock RunManager."""
    with patch("app.api.v1.chat_runs.get_run_manager") as factory:
        rm = MagicMock()
        rm.available = True
        factory.return_value = rm
        yield rm


# ---------------------------------------------------------------------------
# GET /api/v1/chat/runs/{run_id}/stream
# ---------------------------------------------------------------------------


class TestStreamEndpoint:
    """SSE stream relay from Redis."""

    def test_stream_returns_events_and_content_type(self, client, mock_rm):
        run_id = str(uuid.uuid4())
        mock_rm.get_status.return_value = "running"

        # First read returns events, second returns [] (simulating done)
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [
                    {"id": "1-0", "data": {"type": "text", "content": "hello"}},
                ]
            # After first batch, mark status as complete
            mock_rm.get_status.return_value = "complete"
            return []

        mock_rm.read_events.side_effect = _side_effect

        resp = client.get(f"/api/v1/chat/runs/{run_id}/stream")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        body = resp.text
        # Should contain the event
        assert '"type": "text"' in body or '"type":"text"' in body
        # Should contain padding (8KB of spaces)
        assert "         " in body  # part of padding
        # Should contain run_status event at the end
        assert "run_status" in body

    def test_stream_404_when_run_not_found(self, client, mock_rm):
        run_id = str(uuid.uuid4())
        mock_rm.get_status.return_value = None

        resp = client.get(f"/api/v1/chat/runs/{run_id}/stream")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_stream_503_when_redis_unavailable(self, client, mock_rm):
        mock_rm.available = False
        run_id = str(uuid.uuid4())

        resp = client.get(f"/api/v1/chat/runs/{run_id}/stream")
        assert resp.status_code == 503

    def test_stream_with_last_id_param(self, client, mock_rm):
        run_id = str(uuid.uuid4())
        mock_rm.get_status.return_value = "complete"
        mock_rm.read_events.return_value = []

        resp = client.get(f"/api/v1/chat/runs/{run_id}/stream", params={"last_id": "5-0"})
        assert resp.status_code == 200
        # Verify read_events was called with the last_id
        mock_rm.read_events.assert_called()
        call_kwargs = mock_rm.read_events.call_args
        assert call_kwargs[1].get("last_id") == "5-0" or (len(call_kwargs[0]) >= 2 and call_kwargs[0][1] == "5-0")


# ---------------------------------------------------------------------------
# POST /api/v1/chat/runs/{run_id}/cancel
# ---------------------------------------------------------------------------


class TestCancelEndpoint:
    """Graceful cancel via RunManager."""

    def test_cancel_returns_200(self, client, mock_rm):
        run_id = str(uuid.uuid4())
        mock_rm.get_status.return_value = "running"

        resp = client.post(f"/api/v1/chat/runs/{run_id}/cancel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cancelling"
        assert data["run_id"] == run_id
        mock_rm.request_cancel.assert_called_once_with(run_id)

    def test_cancel_404_when_run_not_found(self, client, mock_rm):
        run_id = str(uuid.uuid4())
        mock_rm.get_status.return_value = None

        resp = client.post(f"/api/v1/chat/runs/{run_id}/cancel")
        assert resp.status_code == 404

    def test_cancel_409_when_not_running(self, client, mock_rm):
        run_id = str(uuid.uuid4())
        mock_rm.get_status.return_value = "complete"

        resp = client.post(f"/api/v1/chat/runs/{run_id}/cancel")
        assert resp.status_code == 409
        assert "not running" in resp.json()["detail"].lower()

    def test_cancel_503_when_redis_unavailable(self, client, mock_rm):
        mock_rm.available = False
        run_id = str(uuid.uuid4())

        resp = client.post(f"/api/v1/chat/runs/{run_id}/cancel")
        assert resp.status_code == 503
