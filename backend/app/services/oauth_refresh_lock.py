"""Owned, fail-closed locks for rotating NetSuite REST refresh tokens."""

import secrets

from app.core.redis_lock import _get_redis

LOCK_SECONDS = 180  # Exceeds the HTTP timeout and the native reader's 45s auth bound.
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"


def acquire(key):
    owner = secrets.token_hex(24)
    try:
        client = _get_redis()
        return owner if client is not None and client.set(key, owner, nx=True, ex=LOCK_SECONDS) else None
    except Exception:
        return None  # Never consume a single-use refresh token without serialization.


def release(key, owner):
    try:
        client = _get_redis()
        if client is not None:
            client.eval(_RELEASE, 1, key, owner)
    except Exception:
        pass  # The bounded lease expires; never delete an unowned lock.
