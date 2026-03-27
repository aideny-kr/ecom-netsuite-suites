"""Short-lived per-conversation result cache for follow-up intelligence.

Stores chartable summaries of query results in Redis. TTL: 30 minutes.
Max 3 results per conversation (evict oldest).
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings

CACHE_TTL_SECONDS = 1800  # 30 minutes
MAX_RESULTS_PER_CONVERSATION = 3
MAX_PREVIEW_ROWS = 50  # Enough for charting/pivoting


@dataclass
class CachedResult:
    message_id: str
    conversation_id: str
    result_type: str  # "suiteql", "financial_report", "bigquery", "saved_search"
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    summary: dict[str, Any] | None = None
    query_text: str = ""
    created_at: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(
            {
                "message_id": self.message_id,
                "conversation_id": self.conversation_id,
                "result_type": self.result_type,
                "columns": self.columns,
                "rows": self.rows[:MAX_PREVIEW_ROWS],
                "row_count": self.row_count,
                "summary": self.summary,
                "query_text": self.query_text,
                "created_at": self.created_at,
            },
            default=str,
        )

    @classmethod
    def from_json(cls, data: str) -> "CachedResult":
        d = json.loads(data)
        return cls(**d)


def _get_redis():
    """Get Redis client. Returns None if Redis unavailable (dev fallback)."""
    try:
        import redis

        return redis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception:
        return None


def _cache_key(conversation_id: str) -> str:
    return f"result_cache:{conversation_id}"


async def cache_result(conversation_id: str, message_id: str, result: CachedResult) -> None:
    """Store a result in the cache. Evicts oldest if > MAX_RESULTS_PER_CONVERSATION."""
    r = _get_redis()
    if not r:
        return

    key = _cache_key(conversation_id)
    r.hset(key, message_id, result.to_json())
    r.expire(key, CACHE_TTL_SECONDS)

    # Evict oldest if over limit
    all_fields = r.hgetall(key)
    if len(all_fields) > MAX_RESULTS_PER_CONVERSATION:
        entries = []
        for mid, raw in all_fields.items():
            try:
                cr = CachedResult.from_json(raw)
                entries.append((mid, cr.created_at))
            except Exception:
                entries.append((mid, 0))
        entries.sort(key=lambda x: x[1])
        to_remove = entries[: len(entries) - MAX_RESULTS_PER_CONVERSATION]
        for mid, _ in to_remove:
            r.hdel(key, mid)


async def get_latest_result(conversation_id: str) -> CachedResult | None:
    """Get the most recent cached result for this conversation."""
    r = _get_redis()
    if not r:
        return None

    key = _cache_key(conversation_id)
    all_fields = r.hgetall(key)
    if not all_fields:
        return None

    latest = None
    latest_time = 0.0
    for raw in all_fields.values():
        try:
            cr = CachedResult.from_json(raw)
            if cr.created_at > latest_time:
                latest = cr
                latest_time = cr.created_at
        except Exception:
            continue
    return latest


async def get_result_by_message(conversation_id: str, message_id: str) -> CachedResult | None:
    """Get a specific cached result by message ID."""
    r = _get_redis()
    if not r:
        return None

    key = _cache_key(conversation_id)
    raw = r.hget(key, message_id)
    if not raw:
        return None
    try:
        return CachedResult.from_json(raw)
    except Exception:
        return None
