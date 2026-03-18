"""Distributed lock using Redis SET NX EX."""

from __future__ import annotations

import logging

import redis

from app.core.config import settings

logger = logging.getLogger(__name__)

_redis: redis.Redis | None = None


def _get_redis() -> redis.Redis | None:
    global _redis
    if _redis is None:
        try:
            _redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
        except Exception:
            logger.warning("redis_lock.connection_failed", exc_info=True)
            return None
    return _redis


def acquire_lock(key: str, timeout: int = 30) -> bool:
    """Acquire a lock. Returns True if acquired, False if already held.

    Falls back to "always acquire" if Redis is unavailable (dev mode).
    """
    r = _get_redis()
    if r is None:
        return True
    try:
        return bool(r.set(key, "1", nx=True, ex=timeout))
    except Exception:
        logger.warning("redis_lock.acquire_failed", exc_info=True)
        return True  # Fail open — proceed without lock


def release_lock(key: str) -> None:
    """Release a lock."""
    r = _get_redis()
    if r is None:
        return
    try:
        r.delete(key)
    except Exception:
        logger.warning("redis_lock.release_failed", exc_info=True)
