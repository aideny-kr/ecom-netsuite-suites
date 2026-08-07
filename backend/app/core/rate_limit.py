"""Redis-backed sliding-window rate limiter.

Uses Redis sorted sets with timestamps as scores for a true sliding window.
Survives restarts and works across multiple process replicas.

Falls back to an in-memory dict if Redis is unavailable. The fallback still
ENFORCES -- it is degraded, not disabled. Failing open would lift the cap at
exactly the moment a runaway loop is the likeliest reason Redis is unwell.

Callers are the named `check_*` helpers below; each owns a key prefix so one
limiter can never consume another's window (a chat flood must not lock the same
user out of logging in).
"""

import logging
import threading
import time
from collections import defaultdict

import redis

from app.core.config import settings

logger = logging.getLogger(__name__)

_redis: redis.Redis | None = None
_fallback: dict[str, list[float]] = defaultdict(list)
# The fallback's read-modify-write is not atomic, and callers now reach it from
# worker threads (asyncio.to_thread at the chat and MCP call sites), so concurrent
# checks could each observe a count below the cap and all be admitted.
_fallback_lock = threading.Lock()

WINDOW_SECONDS = 60
MAX_ATTEMPTS = 10

# Per-user chat turns per minute. Burst-only by design (operator decision
# 2026-08-06): it bounds retry loops and scripted clients without ever telling a
# legitimate heavy recon/report user "no more today", which a daily quota would.
CHAT_BURST_PER_MINUTE = settings.CHAT_BURST_PER_MINUTE

_KEY_ROOT = "ratelimit:"
_LOGIN_PREFIX = f"{_KEY_ROOT}login:"
_CHAT_PREFIX = f"{_KEY_ROOT}chat:"
_MCP_PREFIX = f"{_KEY_ROOT}mcp:"


def _get_redis() -> redis.Redis | None:
    global _redis
    if _redis is not None:
        return _redis
    try:
        _redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
        _redis.ping()
        return _redis
    except Exception:
        _redis = None
        return None


def check_limit(key: str, limit: int, window_seconds: int = WINDOW_SECONDS) -> bool:
    """Return True if the request is allowed, False if rate-limited.

    Two different Redis failures, two different answers:

    * **Unreachable at connect** -- Redis is simply absent (dev, or a full outage
      we already know about). Degrade to the per-process window: it under-counts
      across replicas but still enforces.
    * **Reachable but the call raised** -- Redis holds a count we could not read.
      DENY. Falling back here would score the request against `_fallback[key]`,
      a counter starting at zero and completely disjoint from what Redis already
      has, handing a client at the cap a fresh full window. That is the cap being
      lifted during flakiness, which is precisely what this module promises not
      to do, and it is how a runaway loop escapes. We also drop the cached client
      so the next call re-attempts and, if Redis is genuinely gone, degrades to
      the fallback cleanly instead of denying forever.
    """
    global _redis
    r = _get_redis()
    if r is None:
        return _check_fallback(key, limit, window_seconds)
    try:
        return _check_redis(r, key, limit, window_seconds)
    except Exception:
        logger.warning("rate_limit.redis_error_denying key_prefix=%s", key.split(":", 2)[:2])
        _redis = None
        return False


def check_login_rate_limit(ip: str) -> bool:
    return check_limit(f"{_LOGIN_PREFIX}{ip}", MAX_ATTEMPTS, WINDOW_SECONDS)


def check_chat_burst_limit(tenant_id: str, user_id: str) -> bool:
    """Per-user, per-tenant cap on chat turns.

    Keyed by both so one noisy user cannot starve their colleagues, and one
    tenant cannot starve another.
    """
    return check_limit(f"{_CHAT_PREFIX}{tenant_id}:{user_id}", settings.CHAT_BURST_PER_MINUTE, WINDOW_SECONDS)


def check_mcp_tool_limit(tenant_id: str, tool_name: str, limit: int) -> bool:
    """Per-tenant, per-tool MCP cap.

    Previously a per-process dict in `app.mcp.governance`, which silently granted
    N times the configured limit across N workers/replicas.
    """
    return check_limit(f"{_MCP_PREFIX}{tenant_id}:{tool_name}", limit, WINDOW_SECONDS)


def _check_redis(r: redis.Redis, key: str, limit: int, window_seconds: int) -> bool:
    """Redis sliding window using sorted sets.

    Note: the attempt is recorded even when it is denied, so a client that keeps
    hammering keeps the window full -- a penalty box. That is deliberate for login
    brute-force and desirable for chat cost control. `_check_fallback` does NOT do
    this, so behaviour under sustained overload differs between the two paths; the
    divergence predates this module's generalisation and is left alone rather than
    quietly changing login lockout semantics.
    """
    now = time.time()
    cutoff = now - window_seconds

    pipe = r.pipeline()
    # Remove expired entries
    pipe.zremrangebyscore(key, "-inf", cutoff)
    # Count remaining entries in window
    pipe.zcard(key)
    # Add current attempt
    pipe.zadd(key, {str(now): now})
    # Set TTL to auto-cleanup
    pipe.expire(key, window_seconds + 1)
    results = pipe.execute()

    count = results[1]  # zcard result before adding current
    if count >= limit:
        return False
    return True


def _per_process_limit(limit: int) -> int:
    """Split a FLEET-WIDE limit into this process's share.

    Every caller's `limit` is fleet-wide: the 2026-08-06 re-baseline multiplied
    TOOL_CONFIGS by the worker count precisely so the shared Redis window enforces
    the real ceiling. Handing that same number to a per-process counter multiplies
    it right back by the worker count -- during a Redis outage netsuite.suiteql
    would allow ~480/min across 4 workers against a pre-re-baseline effective
    ~120/min, i.e. the outage would be 4x LOOSER than before any of this work.
    """
    return max(1, limit // max(1, settings.WEB_CONCURRENCY))


def _check_fallback(key: str, limit: int, window_seconds: int) -> bool:
    """In-memory fallback, enforcing this process's share of the fleet-wide limit."""
    limit = _per_process_limit(limit)
    now = time.monotonic()
    cutoff = now - window_seconds
    with _fallback_lock:
        _fallback[key] = [t for t in _fallback[key] if t > cutoff]
        attempts = _fallback[key]

        if len(attempts) >= limit:
            return False

        attempts.append(now)
        return True


def reset_rate_limits(prefix: str = _KEY_ROOT) -> None:
    """Clear rate limit state. Used in tests, and by governance's per-tenant reset."""
    global _redis
    r = _get_redis()
    if r:
        try:
            cursor = 0
            while True:
                cursor, keys = r.scan(cursor, match=f"{prefix}*", count=100)
                if keys:
                    r.delete(*keys)
                if cursor == 0:
                    break
        except Exception:
            logger.warning("rate_limit.reset_scan_failed prefix=%s", prefix)
    with _fallback_lock:
        for key in [k for k in _fallback if k.startswith(prefix)]:
            del _fallback[key]
    if prefix == _KEY_ROOT:
        _redis = None
