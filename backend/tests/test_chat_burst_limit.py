"""Chat-turn burst limiting, and replica-safety of the shared sliding window.

Two defects are pinned here:

1. `send_message` had no rate limit at all. Platform-billed tenants spend OUR
   Anthropic key, so a retry loop or scripted client ran unbounded until a human
   noticed the bill. Operator decision 2026-08-06: per-minute burst only, no daily
   quota -- bound runaway loops without telling a legitimate heavy recon/report user
   "no more today".

2. `governance._rate_limits` was a per-process dict, so the configured per-tenant,
   per-tool MCP limits were effectively N-times higher with N workers/replicas. The
   window now lives in Redis, which is what makes the configured number the real one.
"""

from __future__ import annotations

import uuid

import pytest

from app.core import rate_limit as rl
from app.mcp.governance import TOOL_CONFIGS, check_rate_limit, reset_rate_limit


class FakeRedis:
    """Minimal sorted-set double covering exactly the ops the limiter uses.

    Stands in for a SHARED store: handing the same instance to two limiter states
    is what "two replicas" means here.
    """

    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}

    def ping(self) -> bool:
        return True

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    def scan(self, cursor, match="*", count=100):
        prefix = match[:-1] if match.endswith("*") else match
        return 0, [k for k in self.zsets if k.startswith(prefix)]

    def delete(self, *keys):
        for key in keys:
            self.zsets.pop(key, None)
        return len(keys)


class FakePipeline:
    def __init__(self, store: FakeRedis) -> None:
        self._store = store
        self._ops: list[tuple] = []

    def zremrangebyscore(self, key, min_, max_):
        self._ops.append(("zrem", key, max_))
        return self

    def zcard(self, key):
        self._ops.append(("zcard", key))
        return self

    def zadd(self, key, mapping):
        self._ops.append(("zadd", key, mapping))
        return self

    def expire(self, key, ttl):
        self._ops.append(("expire", key, ttl))
        return self

    def execute(self):
        results = []
        for op in self._ops:
            kind, key = op[0], op[1]
            bucket = self._store.zsets.setdefault(key, {})
            if kind == "zrem":
                cutoff = op[2]
                for member, score in list(bucket.items()):
                    if score <= cutoff:
                        del bucket[member]
                results.append(None)
            elif kind == "zcard":
                results.append(len(bucket))
            elif kind == "zadd":
                bucket.update(op[2])
                results.append(1)
            else:
                results.append(True)
        self._ops = []
        return results


@pytest.fixture(autouse=True)
def _clean_limiter_state():
    rl.reset_rate_limits()
    yield
    rl.reset_rate_limits()


@pytest.fixture
def no_redis(monkeypatch):
    """Force the in-memory fallback path."""
    monkeypatch.setattr(rl, "_get_redis", lambda: None)


@pytest.fixture
def shared_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(rl, "_get_redis", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# Chat burst limit
# ---------------------------------------------------------------------------


def test_chat_burst_allows_up_to_the_limit(no_redis):
    tenant, user = str(uuid.uuid4()), str(uuid.uuid4())
    for _ in range(rl.CHAT_BURST_PER_MINUTE):
        assert rl.check_chat_burst_limit(tenant, user) is True


def test_chat_burst_denies_past_the_limit(no_redis):
    tenant, user = str(uuid.uuid4()), str(uuid.uuid4())
    for _ in range(rl.CHAT_BURST_PER_MINUTE):
        rl.check_chat_burst_limit(tenant, user)
    assert rl.check_chat_burst_limit(tenant, user) is False


def test_chat_burst_is_per_user_not_per_tenant(no_redis):
    tenant = str(uuid.uuid4())
    noisy, quiet = str(uuid.uuid4()), str(uuid.uuid4())
    for _ in range(rl.CHAT_BURST_PER_MINUTE):
        rl.check_chat_burst_limit(tenant, noisy)
    assert rl.check_chat_burst_limit(tenant, noisy) is False
    assert rl.check_chat_burst_limit(tenant, quiet) is True, "one user must not starve their colleagues"


def test_chat_burst_is_per_tenant(no_redis):
    user = str(uuid.uuid4())
    tenant_a, tenant_b = str(uuid.uuid4()), str(uuid.uuid4())
    for _ in range(rl.CHAT_BURST_PER_MINUTE):
        rl.check_chat_burst_limit(tenant_a, user)
    assert rl.check_chat_burst_limit(tenant_b, user) is True


def test_chat_burst_does_not_share_a_window_with_login(no_redis):
    """Distinct key prefixes -- a chat flood must not lock the user out of logging in."""
    tenant, user = str(uuid.uuid4()), str(uuid.uuid4())
    for _ in range(rl.CHAT_BURST_PER_MINUTE):
        rl.check_chat_burst_limit(tenant, user)
    assert rl.check_chat_burst_limit(tenant, user) is False
    assert rl.check_login_rate_limit(user) is True


# ---------------------------------------------------------------------------
# Replica safety -- the actual governance defect
# ---------------------------------------------------------------------------


def test_chat_burst_window_is_shared_across_replicas(shared_redis):
    """Replica A exhausts the window; replica B must see it as exhausted.

    Dropping the in-memory fallback between calls is what makes this a second
    process rather than the same one.
    """
    tenant, user = str(uuid.uuid4()), str(uuid.uuid4())
    for _ in range(rl.CHAT_BURST_PER_MINUTE):
        assert rl.check_chat_burst_limit(tenant, user) is True

    rl._fallback.clear()  # replica B: fresh process memory, same Redis

    assert rl.check_chat_burst_limit(tenant, user) is False


def test_mcp_tool_window_is_shared_across_replicas(shared_redis):
    """The per-process dict meant N replicas granted N times the configured limit."""
    tenant = str(uuid.uuid4())
    tool = "netsuite.suiteql"
    limit = TOOL_CONFIGS[tool]["rate_limit_per_minute"]

    for _ in range(limit):
        assert check_rate_limit(tenant, tool) is True

    rl._fallback.clear()  # replica B

    assert check_rate_limit(tenant, tool) is False


def test_mcp_tool_limits_still_isolate_tenants(shared_redis):
    tenant_a, tenant_b = str(uuid.uuid4()), str(uuid.uuid4())
    tool = "recon.run"
    limit = TOOL_CONFIGS[tool]["rate_limit_per_minute"]

    for _ in range(limit):
        check_rate_limit(tenant_a, tool)

    assert check_rate_limit(tenant_a, tool) is False
    assert check_rate_limit(tenant_b, tool) is True


def test_reset_rate_limit_clears_a_single_tenant(shared_redis):
    tenant = str(uuid.uuid4())
    tool = "recon.run"
    limit = TOOL_CONFIGS[tool]["rate_limit_per_minute"]
    for _ in range(limit):
        check_rate_limit(tenant, tool)
    assert check_rate_limit(tenant, tool) is False

    reset_rate_limit(tenant)

    assert check_rate_limit(tenant, tool) is True


# ---------------------------------------------------------------------------
# Redis-down posture: fail CLOSED (keep enforcing), never fail open
# ---------------------------------------------------------------------------


def test_limits_still_enforced_when_redis_is_down(no_redis):
    """Their limiter fails open. Ours must not -- Redis down is exactly when a
    runaway loop is most likely to be the reason Redis is down."""
    tenant = str(uuid.uuid4())
    tool = "recon.run"
    limit = TOOL_CONFIGS[tool]["rate_limit_per_minute"]
    for _ in range(limit):
        assert check_rate_limit(tenant, tool) is True
    assert check_rate_limit(tenant, tool) is False
