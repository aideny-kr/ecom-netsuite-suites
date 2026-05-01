"""Short-lived per-conversation result cache for follow-up intelligence.

Stores chartable summaries of query results in Redis. TTL: 30 minutes.
Cap of 6 entries per conversation, with the most recent of each ``result_type``
pinned so a SuiteQL/BigQuery turn between two pricing turns can't evict the
pricing state.
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings

CACHE_TTL_SECONDS = 1800  # 30 minutes
MAX_RESULTS_PER_CONVERSATION = 6
MAX_PREVIEW_ROWS = 50  # Enough for charting/pivoting


@dataclass
class CachedResult:
    message_id: str
    conversation_id: str
    result_type: str  # "suiteql" | "financial_report" | "bigquery" | "saved_search" | "pricing"
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    summary: dict[str, Any] | None = None
    query_text: str = ""
    payload: dict[str, Any] | None = None  # typed per-result_type state (e.g., pricing_state)
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
                "payload": self.payload,
                "created_at": self.created_at,
            },
            default=str,
        )

    @classmethod
    def from_json(cls, data: str) -> "CachedResult":
        d = json.loads(data)
        # `payload` is optional; pre-payload entries omit the key entirely.
        d.setdefault("payload", None)
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


def _cache_result_sync(conversation_id: str, message_id: str, result: CachedResult) -> None:
    """Synchronous cache write — used by the orchestrator's intercept callback
    so same-turn follow-ups (e.g. ``pricing_export`` → ``pricing_to_sheets``
    in one assistant message) can read the entry via
    ``get_latest_result_by_type`` before the agent loop completes.

    The Redis client is synchronous internally; this helper is pure-sync so
    callers don't need an event loop.
    """
    r = _get_redis()
    if not r:
        return

    key = _cache_key(conversation_id)
    r.hset(key, message_id, result.to_json())
    r.expire(key, CACHE_TTL_SECONDS)

    all_fields = r.hgetall(key)
    if len(all_fields) <= MAX_RESULTS_PER_CONVERSATION:
        return

    # Decode all entries for eviction reasoning.
    entries: list[tuple[str, CachedResult]] = []
    for mid, raw in all_fields.items():
        try:
            entries.append((mid, CachedResult.from_json(raw)))
        except Exception:
            # Treat undecodable entries as oldest unpinned junk.
            entries.append(
                (
                    mid,
                    CachedResult(
                        message_id=mid,
                        conversation_id=conversation_id,
                        result_type="_unknown",
                        columns=[],
                        rows=[],
                        row_count=0,
                        created_at=0.0,
                    ),
                )
            )

    # Pin the most recent entry of each result_type.
    pinned_ids: set[str] = set()
    by_type: dict[str, tuple[str, float]] = {}
    for mid, cr in entries:
        prev = by_type.get(cr.result_type)
        if prev is None or cr.created_at > prev[1]:
            by_type[cr.result_type] = (mid, cr.created_at)
    for mid, _ in by_type.values():
        pinned_ids.add(mid)

    # Evict from the unpinned set, oldest first.
    unpinned = [(mid, cr.created_at) for mid, cr in entries if mid not in pinned_ids]
    unpinned.sort(key=lambda x: x[1])

    over = len(entries) - MAX_RESULTS_PER_CONVERSATION
    to_remove = [mid for mid, _ in unpinned[:over]]

    # If pinning leaves us still over the cap (every entry is a different type
    # and we are over the cap), drop oldest pinned as a fallback.
    if len(to_remove) < over:
        remaining = [(mid, cr.created_at) for mid, cr in entries if mid not in to_remove]
        remaining.sort(key=lambda x: x[1])
        extra_needed = over - len(to_remove)
        to_remove.extend(mid for mid, _ in remaining[:extra_needed])

    for mid in to_remove:
        r.hdel(key, mid)


async def cache_result(conversation_id: str, message_id: str, result: CachedResult) -> None:
    """Async wrapper around ``_cache_result_sync`` — kept for callers that
    already live in an async context. Eviction policy: when count exceeds
    ``MAX_RESULTS_PER_CONVERSATION``, pin the most recent entry of each
    ``result_type`` and evict the oldest entry from the unpinned set.
    """
    _cache_result_sync(conversation_id, message_id, result)


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


async def get_latest_result_by_type(conversation_id: str, result_type: str) -> CachedResult | None:
    """Get the most recent cached result of a specific ``result_type``.

    Use this for follow-ups that must operate on a typed prior turn (e.g.,
    ``pricing_revise`` reads the latest pricing entry, not whatever query the
    user ran in between).
    """
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
        except Exception:
            continue
        if cr.result_type != result_type:
            continue
        if cr.created_at > latest_time:
            latest = cr
            latest_time = cr.created_at
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
