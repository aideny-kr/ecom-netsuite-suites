"""Redis-backed JWT denylist keyed by JTI.

Tokens are stored with TTL matching their remaining expiry time,
so Redis automatically cleans them up. Survives restarts and
works across multiple process replicas.

Falls back to in-memory dict if Redis is unavailable (development only).
"""

import time

import redis

from app.core.config import settings

_redis: redis.Redis | None = None
_fallback: dict[str, float] = {}  # in-memory fallback for tests/dev


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


_DENYLIST_PREFIX = "jwt:denied:"


def revoke_token(jti: str, exp: float) -> None:
    """Add a JTI to the denylist. `exp` is the token's expiry as a Unix timestamp."""
    ttl = max(int(exp - time.time()), 1)
    r = _get_redis()
    if r:
        r.setex(f"{_DENYLIST_PREFIX}{jti}", ttl, "1")
    else:
        _fallback[jti] = exp


def is_revoked(jti: str) -> bool:
    """Check if a JTI has been revoked."""
    r = _get_redis()
    if r:
        return r.exists(f"{_DENYLIST_PREFIX}{jti}") > 0
    return jti in _fallback


def reset_denylist() -> None:
    """Clear all denylist state. Used in tests."""
    global _redis
    r = _get_redis()
    if r:
        # Delete all denylist keys
        cursor = 0
        while True:
            cursor, keys = r.scan(cursor, match=f"{_DENYLIST_PREFIX}*", count=100)
            if keys:
                r.delete(*keys)
            if cursor == 0:
                break
    _fallback.clear()
    _redis = None  # Force reconnect on next use
