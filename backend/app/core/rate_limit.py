"""Redis-backed sliding-window rate limiter for login attempts.

Uses Redis sorted sets with timestamps as scores for a true sliding window.
Survives restarts and works across multiple process replicas.

Falls back to in-memory dict if Redis is unavailable (development only).
"""

import time
from collections import defaultdict

import redis

from app.core.config import settings

_redis: redis.Redis | None = None
_fallback: dict[str, list[float]] = defaultdict(list)

WINDOW_SECONDS = 60
MAX_ATTEMPTS = 10

_RATE_LIMIT_PREFIX = "ratelimit:login:"


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


def check_login_rate_limit(ip: str) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    r = _get_redis()
    if r:
        return _check_redis(r, ip)
    return _check_fallback(ip)


def _check_redis(r: redis.Redis, ip: str) -> bool:
    """Redis sliding window using sorted sets."""
    key = f"{_RATE_LIMIT_PREFIX}{ip}"
    now = time.time()
    cutoff = now - WINDOW_SECONDS

    pipe = r.pipeline()
    # Remove expired entries
    pipe.zremrangebyscore(key, "-inf", cutoff)
    # Count remaining entries in window
    pipe.zcard(key)
    # Add current attempt
    pipe.zadd(key, {str(now): now})
    # Set TTL to auto-cleanup
    pipe.expire(key, WINDOW_SECONDS + 1)
    results = pipe.execute()

    count = results[1]  # zcard result before adding current
    if count >= MAX_ATTEMPTS:
        return False
    return True


def _check_fallback(ip: str) -> bool:
    """In-memory fallback for development/testing."""
    now = time.monotonic()
    attempts = _fallback[ip]
    cutoff = now - WINDOW_SECONDS
    _fallback[ip] = [t for t in attempts if t > cutoff]
    attempts = _fallback[ip]

    if len(attempts) >= MAX_ATTEMPTS:
        return False

    attempts.append(now)
    return True


def reset_rate_limits() -> None:
    """Clear all rate limit state. Used in tests."""
    global _redis
    r = _get_redis()
    if r:
        cursor = 0
        while True:
            cursor, keys = r.scan(cursor, match=f"{_RATE_LIMIT_PREFIX}*", count=100)
            if keys:
                r.delete(*keys)
            if cursor == 0:
                break
    _fallback.clear()
    _redis = None
