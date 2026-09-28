"""The chat stream carries group preparation progress as its own event, never as the turn's result."""

from app.services.chat import orchestrator


def test_preparation_progress_is_its_own_stream_event():
    snapshot = {"checked": 1, "total": 2, "ready": 1, "set_aside": [], "now": ["R2"]}
    assert orchestrator.progress_event(snapshot) == {"type": "preparation_progress", "data": snapshot}
