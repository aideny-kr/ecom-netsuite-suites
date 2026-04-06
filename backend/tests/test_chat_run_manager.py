"""Tests for chat RunManager — Redis-backed run state and event streams."""


import pytest
import redis

from app.services.chat.run_manager import RunManager, get_run_manager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REDIS_URL = "redis://localhost:6379/0"


def _redis_available() -> bool:
    try:
        r = redis.from_url(REDIS_URL, decode_responses=True)
        r.ping()
        return True
    except Exception:
        return False


skip_no_redis = pytest.mark.skipif(
    not _redis_available(), reason="Redis not available"
)


@pytest.fixture
def mgr():
    """Fresh RunManager with cleanup."""
    m = RunManager(redis_url=REDIS_URL)
    yield m
    # Cleanup: delete all keys created during test
    r = m._redis
    if r:
        for key in r.scan_iter("chat:run:test-*"):
            r.delete(key)
        for key in r.scan_iter("chat:session:test-*"):
            r.delete(key)


# ---------------------------------------------------------------------------
# TestRunLifecycle
# ---------------------------------------------------------------------------


@skip_no_redis
class TestRunLifecycle:
    def test_create_run_sets_status_running(self, mgr: RunManager):
        mgr.create_run("test-run-1", "test-session-1")
        assert mgr.get_status("test-run-1") == "running"

    def test_set_status_complete(self, mgr: RunManager):
        mgr.create_run("test-run-2", "test-session-2")
        mgr.set_status("test-run-2", "complete")
        assert mgr.get_status("test-run-2") == "complete"

    def test_get_status_missing_run(self, mgr: RunManager):
        assert mgr.get_status("test-run-nonexistent") is None


# ---------------------------------------------------------------------------
# TestEventStream
# ---------------------------------------------------------------------------


@skip_no_redis
class TestEventStream:
    def test_write_and_read_events(self, mgr: RunManager):
        mgr.create_run("test-run-ev1", "test-session-ev1")
        mgr.write_event("test-run-ev1", {"type": "token", "data": "hello"})
        mgr.write_event("test-run-ev1", {"type": "token", "data": "world"})

        events = mgr.read_events("test-run-ev1", last_id="0-0", count=10)
        assert len(events) == 2
        assert events[0]["data"]["type"] == "token"
        assert events[0]["data"]["data"] == "hello"
        assert events[1]["data"]["data"] == "world"
        # Each event has a stream id
        assert "id" in events[0]

    def test_cursor_based_reading(self, mgr: RunManager):
        mgr.create_run("test-run-ev2", "test-session-ev2")
        mgr.write_event("test-run-ev2", {"type": "a", "seq": "1"})
        mgr.write_event("test-run-ev2", {"type": "b", "seq": "2"})
        mgr.write_event("test-run-ev2", {"type": "c", "seq": "3"})

        # Read first 2
        batch1 = mgr.read_events("test-run-ev2", last_id="0-0", count=2)
        assert len(batch1) == 2

        # Read from cursor — should get remaining
        cursor = batch1[-1]["id"]
        batch2 = mgr.read_events("test-run-ev2", last_id=cursor, count=10)
        assert len(batch2) == 1
        assert batch2[0]["data"]["seq"] == "3"

    def test_empty_stream(self, mgr: RunManager):
        mgr.create_run("test-run-ev3", "test-session-ev3")
        events = mgr.read_events("test-run-ev3", last_id="0-0", count=10)
        assert events == []

    def test_read_nonexistent_stream(self, mgr: RunManager):
        events = mgr.read_events("test-run-no-stream", last_id="0-0", count=10)
        assert events == []


# ---------------------------------------------------------------------------
# TestCancel
# ---------------------------------------------------------------------------


@skip_no_redis
class TestCancel:
    def test_request_cancel(self, mgr: RunManager):
        mgr.create_run("test-run-c1", "test-session-c1")
        assert not mgr.is_cancelled("test-run-c1")
        mgr.request_cancel("test-run-c1")
        assert mgr.is_cancelled("test-run-c1")
        assert mgr.get_status("test-run-c1") == "cancelled"

    def test_cancel_nonexistent(self, mgr: RunManager):
        # Should not raise
        mgr.request_cancel("test-run-no-exist")
        assert mgr.is_cancelled("test-run-no-exist")


# ---------------------------------------------------------------------------
# TestSessionRunGuard
# ---------------------------------------------------------------------------


@skip_no_redis
class TestSessionRunGuard:
    def test_active_run_tracking(self, mgr: RunManager):
        mgr.create_run("test-run-s1", "test-session-s1")
        assert mgr.get_active_run("test-session-s1") == "test-run-s1"

    def test_clear_active_run(self, mgr: RunManager):
        mgr.create_run("test-run-s2", "test-session-s2")
        mgr.clear_active_run("test-session-s2")
        assert mgr.get_active_run("test-session-s2") is None

    def test_no_active_run(self, mgr: RunManager):
        assert mgr.get_active_run("test-session-none") is None

    def test_new_run_replaces_old(self, mgr: RunManager):
        mgr.create_run("test-run-s3a", "test-session-s3")
        mgr.create_run("test-run-s3b", "test-session-s3")
        assert mgr.get_active_run("test-session-s3") == "test-run-s3b"


# ---------------------------------------------------------------------------
# TestRedisUnavailable
# ---------------------------------------------------------------------------


class TestRedisUnavailable:
    def test_bad_url_available_false(self):
        mgr = RunManager(redis_url="redis://localhost:59999/0")
        assert mgr.available is False

    def test_methods_are_noops(self):
        mgr = RunManager(redis_url="redis://localhost:59999/0")
        # None of these should raise
        mgr.create_run("x", "y")
        assert mgr.get_status("x") is None
        assert mgr.get_active_run("y") is None
        assert mgr.read_events("x", "0-0") == []
        mgr.write_event("x", {"foo": "bar"})
        mgr.request_cancel("x")
        assert not mgr.is_cancelled("x")
        mgr.clear_active_run("y")
        mgr.set_status("x", "complete")


# ---------------------------------------------------------------------------
# TestSingleton
# ---------------------------------------------------------------------------


@skip_no_redis
class TestSingleton:
    def test_get_run_manager_returns_same_instance(self):
        a = get_run_manager()
        b = get_run_manager()
        assert a is b
