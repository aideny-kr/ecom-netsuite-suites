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
from collections import defaultdict, deque

import redis

from app.core.config import settings

logger = logging.getLogger(__name__)

_redis: redis.Redis | None = None

# Redis health, trip condition ONE: error RATE inside a window. A streak-only version
# (first attempt) reset on every success, so a Redis failing only SOME commands never
# reached the threshold -- it denied the failing half of every request forever while
# reporting a streak of 1. These timestamps age out on their own, so an interleaved
# success does not erase the evidence.
_redis_error_times: deque[float] = deque()
# Consecutive failures, reset by any success. This is the SECOND trip condition, and
# it exists because the rate window alone has a blind spot: on a quiet tenant a
# sustained fault produces errors further apart than the window, so they age out
# before the threshold is reached and every request is denied indefinitely -- the
# deny-forever bug, hiding below a traffic threshold (gate round 8).
#
# Neither condition subsumes the other. The rate catches an INTERMITTENT fault that a
# consecutive counter cannot see (a success resets it); the consecutive count catches
# a SUSTAINED fault at a request rate the window cannot see. Both, or one of the two
# real failure modes goes unhandled.
_redis_consecutive_errors: int = 0
# Monotonic deadline. While in the future, callers skip Redis ENTIRELY. That is also
# the fix for the reconnect storm: the error path drops the cached client, so without
# this every subsequent request re-paid a full connect+PING against a Redis already
# known to be sick, on the hottest path in the app.
_redis_degraded_until: float = 0.0
_health_lock = threading.Lock()

# Errors within REDIS_ERROR_WINDOW_SECONDS before we stop calling it a blip.
# Denying the blip is deliberate: falling back on the first error hands a client
# sitting at the cap a fresh empty counter. Denying indefinitely is not -- that is an
# outage, not a guardrail.
REDIS_ERROR_DEGRADE_AFTER = 3
REDIS_ERROR_WINDOW_SECONDS = 10.0
# How long to stay on the in-memory window before trying Redis again. Bounded so a
# transient incident cannot permanently abandon the shared window.
REDIS_DEGRADE_COOLDOWN_SECONDS = 30.0

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
        # Timeouts are mandatory here, not tuning. This module sits on the hot path of
        # every chat turn and every MCP tool call, and redis-py defaults to blocking
        # forever -- so a blackholed endpoint (SG change, dead NAT, Upstash incident)
        # is an indefinite hang, not a slow call. The asyncio.to_thread wrappers at the
        # call sites do not save us: they relocate the hang into the shared default
        # executor, which has only min(32, cpu+4) threads for the whole process.
        _redis = redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
        )
        _redis.ping()
        return _redis
    except Exception:
        _redis = None
        return None


def check_limit(
    key: str,
    limit: int,
    window_seconds: int = WINDOW_SECONDS,
    *,
    fleet_wide: bool = False,
    deny_on_error: bool = True,
) -> bool:
    """Return True if the request is allowed, False if rate-limited.

    `fleet_wide` says whether `limit` is a whole-fleet number or a per-process one,
    and ONLY affects the fallback path (Redis is shared by definition). Getting this
    wrong in either direction is a real bug, so every caller states it explicitly
    rather than inheriting a default that happens to suit one of them.

    Two different Redis failures, two different answers:

    * **Unreachable at connect** -- Redis is simply absent (dev, or a full outage
      we already know about). Degrade to the per-process window: it under-counts
      across replicas but still enforces.
    * **Reachable but the call raised** -- Redis holds a count we could not read.
      DENY, but only while the fault still looks transient. Falling back on the
      first error would score the request against `_fallback[key]`, a counter
      starting at zero and completely disjoint from what Redis already has,
      handing a client at the cap a fresh full window -- the cap lifted during
      exactly the flakiness this module promises it will not lift for.

      We stop calling it transient once EITHER trip condition fires (see
      `_record_redis_failure`: a burst rate, or a run of consecutive failures), then
      degrade to the per-process window for REDIS_DEGRADE_COOLDOWN_SECONDS and dial
      Redis again after that. Both the connect-failure and command-failure paths feed
      the breaker, so a dead endpoint stops being re-dialled as well as stops denying.

      Three earlier versions of this branch got it wrong in three different ways, and
      the sequence is the argument for the current shape:
        1. Dropping the cached client "degrades cleanly instead of denying forever" --
           true only when Redis is UNREACHABLE. A reachable Redis whose commands fail
           (quota, OOM, MISCONF, ACL) re-connects and re-pings fine every time, so it
           denied every request until the process restarted.
        2. Counting CONSECUTIVE errors, reset on success -- defeated by a partial
           fault: at a 50% error rate the streak never passed 1, so half of all
           traffic was denied indefinitely.
        3. Counting the error RATE alone -- defeated by low traffic: on a quiet
           tenant a sustained fault produces errors further apart than the window, so
           they aged out before the threshold and deny-forever came back below a
           traffic threshold.
      Each fix removed one failure mode and left another, which is why the breaker now
      carries both conditions rather than the "better" one.

    `deny_on_error` is the availability decision, and it belongs to the CALLER, not
    to this function. A cost guardrail may reasonably deny a burst it cannot price.
    Authentication may not: denying there turns a cache fault into a total login
    outage, so login degrades on the first error instead.
    """
    global _redis
    # Already known sick: use the in-memory window and do not dial Redis at all.
    if time.monotonic() < _redis_degraded_until:
        return _check_fallback(key, limit, window_seconds, fleet_wide=fleet_wide)

    r = _get_redis()
    if r is None:
        # UNREACHABLE at connect. This must feed the breaker too, or it becomes a
        # retry storm: _get_redis swallows the exception, so without recording the
        # failure every subsequent request re-paid a full TCP connect + PING against
        # a dead endpoint -- half a second each, on the hot path of every chat turn
        # and MCP tool call, forever (gate round 8). The ANSWER here is unchanged
        # (degrade); what changes is that we stop dialling.
        _record_redis_failure(key, denying=False)
        return _check_fallback(key, limit, window_seconds, fleet_wide=fleet_wide)
    try:
        allowed = _check_redis(r, key, limit, window_seconds)
    except Exception:
        # Reachable but the command failed. Deny while it still looks transient --
        # every caller's errors feed the SAME breaker on purpose, since there is one
        # Redis and a login failure is evidence chat's next call will fail too.
        degraded = _record_redis_failure(key, denying=deny_on_error)
        _redis = None
        if degraded or not deny_on_error:
            return _check_fallback(key, limit, window_seconds, fleet_wide=fleet_wide)
        return False
    _mark_redis_healthy()
    return allowed


def _record_redis_failure(key: str, *, denying: bool) -> bool:
    """Record a Redis failure and report whether the breaker is now open.

    TWO trip conditions, because neither covers the other's failure mode:

    * **rate** -- REDIS_ERROR_DEGRADE_AFTER errors inside REDIS_ERROR_WINDOW_SECONDS.
      Catches an INTERMITTENT fault, which a consecutive counter cannot see because
      the interleaved successes keep resetting it.
    * **consecutive** -- the same number of failures in a row, however slowly they
      arrive. Catches a SUSTAINED fault on a quiet tenant, which the rate window
      cannot see because the errors age out before three accumulate. That gap is how
      deny-forever survived round 7 below a traffic threshold.
    """
    global _redis_degraded_until, _redis_consecutive_errors
    now = time.monotonic()
    with _health_lock:
        _redis_error_times.append(now)
        cutoff = now - REDIS_ERROR_WINDOW_SECONDS
        while _redis_error_times and _redis_error_times[0] < cutoff:
            _redis_error_times.popleft()
        _redis_consecutive_errors += 1
        by_rate = len(_redis_error_times) >= REDIS_ERROR_DEGRADE_AFTER
        by_streak = _redis_consecutive_errors >= REDIS_ERROR_DEGRADE_AFTER
        tripped = by_rate or by_streak
        if tripped:
            _redis_degraded_until = now + REDIS_DEGRADE_COOLDOWN_SECONDS
            _redis_error_times.clear()
            _redis_consecutive_errors = 0
        recent, streak = len(_redis_error_times), _redis_consecutive_errors
    logger.warning(
        "rate_limit.redis_error key_prefix=%s recent=%d streak=%d action=%s%s",
        key.split(":", 2)[:2],
        recent,
        streak,
        "degrade" if (tripped or not denying) else "deny",
        f" trip={'rate' if by_rate else 'streak'}" if tripped else "",
    )
    return tripped


def _mark_redis_healthy() -> None:
    """Clear the consecutive counter after a good call.

    Only the streak resets. The rate window ages out on its own and deliberately
    survives a success -- erasing it there is what blinded the first version of this
    breaker to intermittent faults.
    """
    global _redis_consecutive_errors
    if _redis_consecutive_errors:
        with _health_lock:
            _redis_consecutive_errors = 0


def check_login_rate_limit(ip: str) -> bool:
    """Per-IP login attempts.

    NOT fleet-wide: MAX_ATTEMPTS is a pre-existing per-process number that was never
    part of the 2026-08-06 MCP re-baseline. Dividing it would have silently cut login
    lockout from 10 attempts to 2 during a Redis outage -- a quiet change to brute-force
    semantics, which this work explicitly promised not to make.

    `deny_on_error=False` for the same reason. When this module gained a fail-closed
    error branch for chat COST, login silently inherited it -- so any Redis fault
    would have returned False here and locked every user out of authenticating. A
    cache must not be able to take down auth. Degrading to the per-process counter
    still enforces the lockout (test_rate_limit_redis_failure.py pins both halves).
    """
    return check_limit(
        f"{_LOGIN_PREFIX}{ip}",
        MAX_ATTEMPTS,
        WINDOW_SECONDS,
        fleet_wide=False,
        deny_on_error=False,
    )


def check_chat_burst_limit(tenant_id: str, user_id: str) -> bool:
    """Per-user, per-tenant cap on chat turns.

    Keyed by both so one noisy user cannot starve their colleagues, and one
    tenant cannot starve another.
    """
    # Fleet-wide: the cap means "N turns a minute for this user", not "N per worker".
    # Caveat: with sticky routing a user pinned to one worker sees only their share
    # during a Redis outage. Fleet-correct beats worker-correct for a cost guardrail.
    # deny_on_error: this is a spend guardrail, so an unreadable count is worth a 429
    # while the fault looks transient. It stops being worth it once the fault is
    # sustained -- see check_limit.
    return check_limit(
        f"{_CHAT_PREFIX}{tenant_id}:{user_id}",
        settings.CHAT_BURST_PER_MINUTE,
        WINDOW_SECONDS,
        fleet_wide=True,
        deny_on_error=True,
    )


def check_mcp_tool_limit(tenant_id: str, tool_name: str, limit: int) -> bool:
    """Per-tenant, per-tool MCP cap.

    Previously a per-process dict in `app.mcp.governance`, which silently granted
    N times the configured limit across N workers/replicas.
    """
    # Fleet-wide: TOOL_CONFIGS was multiplied by the worker count on 2026-08-06
    # precisely so the shared window enforces the real ceiling.
    #
    # Behaviour change worth knowing: the old per-process dict did NOT record a denied
    # call, so a capped tool recovered as soon as the original burst aged out. The Redis
    # path records denials (the penalty box), so a client that keeps retrying keeps the
    # window full. Kept deliberately -- for a cost guardrail, "retrying while capped
    # extends the cap" is the behaviour we want. It does mean the Redis and fallback
    # paths differ under sustained overload; documented in _check_redis.
    return check_limit(
        f"{_MCP_PREFIX}{tenant_id}:{tool_name}",
        limit,
        WINDOW_SECONDS,
        fleet_wide=True,
        deny_on_error=True,
    )


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


def _check_fallback(key: str, limit: int, window_seconds: int, *, fleet_wide: bool = False) -> bool:
    """In-memory fallback. Splits the limit only when it is a fleet-wide number."""
    if fleet_wide:
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
    global _redis, _redis_degraded_until, _redis_consecutive_errors
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
        # Health state is limiter state too. Leaving it set meant a full reset did not
        # actually restore the module: a process (or a test) that had already tripped
        # the degrade threshold stayed on the fallback against a healthy Redis.
        with _health_lock:
            _redis_error_times.clear()
            _redis_degraded_until = 0.0
            _redis_consecutive_errors = 0
