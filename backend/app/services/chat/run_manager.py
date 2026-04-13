"""Redis-backed run manager for background chat execution.

Manages run state (status, cancel flags) and event streams via Redis Streams.
Falls back to no-ops when Redis is unavailable (development without Redis).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import redis

from app.core.config import settings

logger = logging.getLogger(__name__)

# TTLs in seconds
_RUN_TTL = 1800  # 30 minutes
_CANCEL_TTL = 300  # 5 minutes


class RunManager:
    """Manages chat run lifecycle and event streams in Redis."""

    def __init__(self, redis_url: str | None = None) -> None:
        url = redis_url or settings.REDIS_URL
        self._redis: redis.Redis | None = None
        try:
            r = redis.from_url(url, decode_responses=True)
            r.ping()
            self._redis = r
        except Exception:
            logger.warning("run_manager: Redis unavailable at %s", url)

    @property
    def available(self) -> bool:
        return self._redis is not None

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    def create_run(self, run_id: str, session_id: str) -> None:
        """Create a new run: set status=running, map session->run."""
        r = self._redis
        if r is None:
            return
        pipe = r.pipeline()
        pipe.set(f"chat:run:{run_id}:status", "running", ex=_RUN_TTL)
        pipe.set(f"chat:run:{run_id}:started_at", str(time.time()), ex=_RUN_TTL)
        pipe.set(f"chat:session:{session_id}:run", run_id, ex=_RUN_TTL)
        pipe.execute()

    def get_started_at(self, run_id: str) -> float | None:
        """Get the start timestamp of a run (Unix epoch)."""
        r = self._redis
        if r is None:
            return None
        val = r.get(f"chat:run:{run_id}:started_at")
        return float(val) if val else None

    def get_status(self, run_id: str) -> str | None:
        """Get the current status of a run."""
        r = self._redis
        if r is None:
            return None
        return r.get(f"chat:run:{run_id}:status")

    def set_status(self, run_id: str, status: str) -> None:
        """Update the status of a run."""
        r = self._redis
        if r is None:
            return
        key = f"chat:run:{run_id}:status"
        r.set(key, status, ex=_RUN_TTL)

    # ------------------------------------------------------------------
    # Session -> run mapping
    # ------------------------------------------------------------------

    def get_active_run(self, session_id: str) -> str | None:
        """Get the active run_id for a session, if any."""
        r = self._redis
        if r is None:
            return None
        return r.get(f"chat:session:{session_id}:run")

    def clear_active_run(self, session_id: str) -> None:
        """Remove the session->run mapping."""
        r = self._redis
        if r is None:
            return
        r.delete(f"chat:session:{session_id}:run")

    # ------------------------------------------------------------------
    # Event stream
    # ------------------------------------------------------------------

    def write_event(self, run_id: str, event: dict[str, Any]) -> str | None:
        """Append an event to the run's Redis Stream. Returns stream ID."""
        r = self._redis
        if r is None:
            return None
        key = f"chat:run:{run_id}:events"
        stream_id = r.xadd(key, {"payload": json.dumps(event)})
        r.expire(key, _RUN_TTL)
        return stream_id

    def read_events(
        self,
        run_id: str,
        last_id: str = "0-0",
        count: int = 100,
        block_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Read events from the run's stream after last_id.

        Returns list of {"id": stream_id, "data": parsed_event_dict}.
        """
        r = self._redis
        if r is None:
            return []

        key = f"chat:run:{run_id}:events"
        try:
            # Use XRANGE for non-blocking, XREAD for blocking
            if block_ms is not None:
                raw = r.xread({key: last_id}, count=count, block=block_ms)
                if not raw:
                    return []
                # xread returns [(stream_name, [(id, fields), ...])]
                entries = raw[0][1]
            else:
                # XRANGE with exclusive start: use '(' prefix for exclusion
                # But the standard approach is to use the next ID after last_id
                # For "0-0" this returns everything; for a real ID we want exclusive
                if last_id == "0-0":
                    start = "-"
                else:
                    start = f"({last_id}"
                entries = r.xrange(key, min=start, max="+", count=count)
        except redis.ResponseError:
            return []

        results = []
        for entry_id, fields in entries:
            try:
                data = json.loads(fields.get("payload", "{}"))
            except (json.JSONDecodeError, TypeError):
                data = fields
            results.append({"id": entry_id, "data": data})
        return results

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def request_cancel(self, run_id: str) -> None:
        """Request cancellation of a run."""
        r = self._redis
        if r is None:
            return
        pipe = r.pipeline()
        pipe.set(f"chat:run:{run_id}:cancel", "1", ex=_CANCEL_TTL)
        pipe.set(f"chat:run:{run_id}:status", "cancelled", ex=_RUN_TTL)
        pipe.execute()

    def is_cancelled(self, run_id: str) -> bool:
        """Check if a run has been cancelled."""
        r = self._redis
        if r is None:
            return False
        return r.get(f"chat:run:{run_id}:cancel") == "1"


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

_instance: RunManager | None = None


def get_run_manager() -> RunManager:
    """Return the module-level RunManager singleton."""
    global _instance
    if _instance is None:
        _instance = RunManager()
    return _instance
