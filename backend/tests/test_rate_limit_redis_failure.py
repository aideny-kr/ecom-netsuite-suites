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


class _FlakyRedis:
    """Reachable, and fails every OTHER command — the realistic partial fault.

    This is the shape a consecutive-error counter cannot see: any interleaved success
    resets it, so the streak never reaches the degrade threshold and the failing half
    of the traffic is denied indefinitely.
    """

    def __init__(self):
        self.n = 0

    def ping(self):
        return True

    def pipeline(self):
        self.n += 1
        if self.n % 2 == 0:
            return _OkPipeline()
        raise ConnectionError("flaky")


def test_an_intermittent_fault_still_degrades(monkeypatch):
    """Gate round 6, major — a regression from round 5's own fix.

    The first version counted CONSECUTIVE errors and reset on every success. Against
    a Redis failing 50% of commands the streak never got past 1, so it never degraded
    and denied half of every tenant's chat turns for as long as the fault lasted.
    Health is now judged by error RATE inside a time window, which a success does not
    erase.
    """
    monkeypatch.setattr(rl, "_get_redis", lambda: _FlakyRedis())
    tenant, user = str(uuid.uuid4()), str(uuid.uuid4())

    verdicts = [rl.check_chat_burst_limit(tenant, user) for _ in range(12)]

    assert verdicts[0] is False, "the first error must still deny — it may be a blip"
    assert verdicts[-1] is True, "a persistent intermittent fault must end in the degraded window, not deny forever"


def test_degraded_mode_stops_touching_redis_at_all(monkeypatch):
    """Also gate round 6: the error path dropped the cached client every time.

    With no negative caching, every subsequent request re-paid a full connect+PING
    against a Redis already known to be unwell — on the hot path of every chat turn
    and every MCP tool call. Once degraded we must not dial it again until the
    cooldown expires.
    """
    erroring = _ErroringRedis()
    monkeypatch.setattr(rl, "_get_redis", lambda: erroring)
    tenant, user = str(uuid.uuid4()), str(uuid.uuid4())

    for _ in range(rl.REDIS_ERROR_DEGRADE_AFTER + 1):
        rl.check_chat_burst_limit(tenant, user)
    calls_when_degraded = erroring.pipeline_calls

    for _ in range(5):
        rl.check_chat_burst_limit(tenant, user)

    assert erroring.pipeline_calls == calls_when_degraded, (
        "degraded mode must skip Redis entirely, not reconnect on every call"
    )


def test_the_degraded_window_expires_so_a_recovered_redis_is_used_again(monkeypatch):
    """Degrading must be temporary, or a blip permanently abandons the shared window."""
    monkeypatch.setattr(rl, "_get_redis", lambda: _ErroringRedis())
    tenant, user = str(uuid.uuid4()), str(uuid.uuid4())
    for _ in range(rl.REDIS_ERROR_DEGRADE_AFTER + 1):
        rl.check_chat_burst_limit(tenant, user)
    assert rl._redis_degraded_until > 0, "should be in the degraded window"

    # Pretend the cooldown elapsed, and Redis came back.
    monkeypatch.setattr(rl, "_redis_degraded_until", 0.0)
    healthy = _HealthyRedis()
    monkeypatch.setattr(rl, "_get_redis", lambda: healthy)

    rl.check_chat_burst_limit(tenant, user)

    assert healthy.pipeline_calls == 1, "must dial the shared window again once recovered"


class _HealthyRedis:
    def __init__(self):
        self.pipeline_calls = 0

    def ping(self):
        return True

    def pipeline(self):
        self.pipeline_calls += 1
        return _OkPipeline()


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
