"""Shared Redis client helpers.

- get_sync_redis(): sync redis.Redis for Celery workers (ProgressEmitter).
  The async variant lives in the modules that need it (redis.asyncio).
"""

from __future__ import annotations

import logging

import redis

from app.core.config import settings

logger = logging.getLogger(__name__)

_sync_redis: redis.Redis | None = None


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
