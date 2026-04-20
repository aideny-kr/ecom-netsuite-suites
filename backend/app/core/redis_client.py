"""Shared Redis client helpers.

- get_sync_redis(): sync redis.Redis for Celery workers (ProgressEmitter).
- get_async_redis(): async redis.asyncio.Redis for FastAPI SSE endpoints.
"""

from __future__ import annotations

import logging

import redis
import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)

_sync_redis: redis.Redis | None = None
_async_redis: aioredis.Redis | None = None


def get_async_redis() -> aioredis.Redis:
    """Async Redis client for FastAPI async endpoints (e.g. SSE streams).

    decode_responses=False (bytes mode) — matches get_sync_redis() so that
    xread consumers can use fields.get(b"data", b"{}").decode() uniformly.

    Lazily initialised on first call; safe for module-level caching in async
    contexts because redis.asyncio.Redis is connection-pool-based.
    """
    global _async_redis
    if _async_redis is None:
        _async_redis = aioredis.Redis.from_url(
            settings.REDIS_URL, decode_responses=False
        )
    return _async_redis


def get_sync_redis() -> redis.Redis:
    """Return a shared sync Redis client. Used by the Celery worker
    (ProgressEmitter) — the async variant is for the web workers.

    decode_responses=False (bytes mode) — stream payloads contain
    JSON-encoded bytes; consumers decode via fields.get(b"data", b"{}").decode().
    This is intentionally different from redis_lock.py's decode_responses=True
    (string mode). Do NOT unify these two factories — the bytes/string
    distinction is load-bearing for xadd/xread consumers.

    Raises redis.RedisError on connection failure. Unlike redis_lock.py, this
    client must NOT fail open — Celery workers need Redis for stream writes.
    """
    global _sync_redis
    if _sync_redis is None:
        try:
            client = redis.from_url(settings.REDIS_URL, decode_responses=False)
            client.ping()
            _sync_redis = client
        except Exception:
            logger.error("redis_client.connection_failed", exc_info=True)
            raise
    return _sync_redis
