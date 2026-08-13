"""What the limiter does when Redis is unwell — the availability half of the contract.

Gate round 5 found three defects that compose into one outage:

  * `check_limit` denied on ANY Redis command error and never recovered. The
    docstring claimed dropping the cached client would "degrade to the fallback
    cleanly instead of denying forever", but that only holds when Redis is
    UNREACHABLE. When it is reachable and merely erroring (Upstash quota, OOM,
    MISCONF, a bad ACL) `_get_redis()` re-connects and re-pings successfully every
    time, so every call took the erroring path again: permanent, fleet-wide denial
    until someone restarted the process.

  * That deny policy lived in the shared `check_limit`, so `check_login_rate_limit`
    inherited a cost guardrail's answer. A chat-cost decision must never be able to
    lock every user out of AUTHENTICATING.

  * The client had no socket timeouts, and this change set moved it onto the hot
    path of every chat turn and every MCP tool call.

The deny-on-blip behaviour is still correct and still pinned (see
test_chat_burst_limit.py::test_redis_raising_mid_call_denies_instead_of_granting_a_fresh_window):
falling back on a single transient error hands a client sitting at the cap a fresh
empty window. The fix is not "stop denying" — it is "stop denying FOREVER".
"""

from __future__ import annotations

import uuid

import pytest

from app.core import rate_limit as rl


class _ErroringRedis:
    """Reachable — `ping` succeeds — but every command raises."""

    def __init__(self):
        self.pipeline_calls = 0

    def ping(self):
        return True

    def pipeline(self):
        self.pipeline_calls += 1
        raise ConnectionError("reachable but erroring (quota/OOM/ACL)")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    rl.reset_rate_limits()
    monkeypatch.setattr(rl.settings, "WEB_CONCURRENCY", 1)
    yield
    rl.reset_rate_limits()


# ---------------------------------------------------------------------------
# Login must never be denied because of a Redis fault
# ---------------------------------------------------------------------------


def test_login_degrades_immediately_instead_of_denying(monkeypatch):
    """A Redis fault must not be able to lock the whole fleet out of logging in.

    Denying here is not a safe default: it is a total authentication outage caused
    by a cache. Login degrades to the per-process counter, which still enforces
    brute-force protection.
    """
    monkeypatch.setattr(rl, "_get_redis", lambda: _ErroringRedis())

    assert rl.check_login_rate_limit("203.0.113.7") is True, (
        "a Redis error must not deny login — the fallback still enforces MAX_ATTEMPTS"
    )


def test_login_fallback_still_enforces_the_lockout(monkeypatch):
    """Degraded, not disabled: brute force is still capped at MAX_ATTEMPTS."""
    monkeypatch.setattr(rl, "_get_redis", lambda: _ErroringRedis())
    ip = "203.0.113.8"

    for _ in range(rl.MAX_ATTEMPTS):
        assert rl.check_login_rate_limit(ip) is True
    assert rl.check_login_rate_limit(ip) is False, "lockout must still bite"


# ---------------------------------------------------------------------------
# Cost guardrails deny the blip, but must not deny forever
# ---------------------------------------------------------------------------


def test_chat_stops_denying_once_the_fault_is_clearly_not_transient(monkeypatch):
    """The bug: permanent denial. Every call re-pinged fine and re-raised.

    A short blip must still deny (that test lives in test_chat_burst_limit.py). A
    SUSTAINED fault must degrade to the in-memory window instead of bricking chat
    for every tenant until a redeploy.
    """
    erroring = _ErroringRedis()
    monkeypatch.setattr(rl, "_get_redis", lambda: erroring)
    tenant, user = str(uuid.uuid4()), str(uuid.uuid4())

    verdicts = [rl.check_chat_burst_limit(tenant, user) for _ in range(rl.REDIS_ERROR_DEGRADE_AFTER + 2)]

    assert verdicts[0] is False, "the first error must still deny — it may be a blip"
    assert any(v is True for v in verdicts), "a sustained Redis fault must degrade to the fallback, not deny forever"


def test_degraded_chat_still_enforces_the_cap(monkeypatch):
    """Degrading must not lift the ceiling — the fallback enforces it per process."""
    monkeypatch.setattr(rl, "_get_redis", lambda: _ErroringRedis())
    tenant, user = str(uuid.uuid4()), str(uuid.uuid4())

    allowed = sum(
        1
        for _ in range(rl.REDIS_ERROR_DEGRADE_AFTER + rl.CHAT_BURST_PER_MINUTE + 10)
        if rl.check_chat_burst_limit(tenant, user)
    )

    assert allowed <= rl.CHAT_BURST_PER_MINUTE, (
        f"degraded path admitted {allowed}, above the {rl.CHAT_BURST_PER_MINUTE} cap"
    )


def test_a_successful_call_resets_the_error_streak(monkeypatch):
    """Otherwise a slow drip of unrelated errors eventually degrades a healthy Redis."""
    erroring = _ErroringRedis()
    monkeypatch.setattr(rl, "_get_redis", lambda: erroring)
    tenant, user = str(uuid.uuid4()), str(uuid.uuid4())

    rl.check_chat_burst_limit(tenant, user)
    assert rl._redis_error_streak > 0

    class _HealthyRedis(_ErroringRedis):
        def pipeline(self):
            return _OkPipeline()

    monkeypatch.setattr(rl, "_get_redis", lambda: _HealthyRedis())
    rl.check_chat_burst_limit(tenant, user)

    assert rl._redis_error_streak == 0, "a healthy call must clear the streak"


class _OkPipeline:
    def zremrangebyscore(self, *a, **k): ...
    def zcard(self, *a, **k): ...
    def zadd(self, *a, **k): ...
    def expire(self, *a, **k): ...
    def execute(self):
        return [0, 0, True, True]


# ---------------------------------------------------------------------------
# The client itself
# ---------------------------------------------------------------------------


def test_redis_client_is_built_with_socket_timeouts(monkeypatch):
    """No timeouts means a blackholed Redis blocks the caller indefinitely.

    This module is now on the hot path of every chat turn and every MCP tool call,
    so an unbounded socket wait is a hang, not a slowdown — and `asyncio.to_thread`
    only moves that hang into the shared default executor, whose threads are few.
    """
    captured = {}

    def _fake_from_url(url, **kwargs):
        captured.update(kwargs)

        class _C:
            def ping(self):
                return True

        return _C()

    monkeypatch.setattr(rl.redis, "from_url", _fake_from_url)
    monkeypatch.setattr(rl, "_redis", None)

    rl._get_redis()

    assert captured.get("socket_connect_timeout"), "no socket_connect_timeout set"
    assert captured.get("socket_timeout"), "no socket_timeout set"
