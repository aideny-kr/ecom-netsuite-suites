"""Shared Redis client helpers.

- get_sync_redis(): sync redis.Redis for Celery workers (ProgressEmitter).
  The async variant lives in the modules that need it (redis.asyncio).
"""

from __future__ import annotations

import redis

from app.core.config import settings

_sync_redis: redis.Redis | None = None


def get_sync_redis() -> redis.Redis:
    """Return a shared sync Redis client. Used by the Celery worker
    (ProgressEmitter) — the async variant is for the web workers."""
    global _sync_redis
    if _sync_redis is None:
        _sync_redis = redis.from_url(settings.REDIS_URL, decode_responses=False)
    return _sync_redis
