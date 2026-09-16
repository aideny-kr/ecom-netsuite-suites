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
        pipe.set(f"chat:run:{run_id}:session", session_id, ex=_RUN_TTL)
        pipe.set(f"chat:session:{session_id}:run", run_id, ex=_RUN_TTL)
        pipe.execute()

    def get_session(self, run_id: str) -> str | None:
        """Owning session, retained after the active-run pointer is cleared."""
        return self._redis.get(f"chat:run:{run_id}:session") if self._redis is not None else None

    def set_outcome(self, run_id: str, outcome: str) -> None:
        if self._redis is not None:
            self._redis.set(f"chat:run:{run_id}:outcome", outcome, ex=_RUN_TTL)

    def get_outcome(self, run_id: str) -> str | None:
        return self._redis.get(f"chat:run:{run_id}:outcome") if self._redis is not None else None

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

    def clear_active_run(self, session_id: str, expected_run_id: str | None = None) -> None:
        """Remove the session->run mapping."""
        r = self._redis
        if r is None:
            return
        key = f"chat:session:{session_id}:run"
        if expected_run_id is None:
            r.delete(key)
        else:
            r.eval(
                "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0",
                1,
                key,
                expected_run_id,
            )

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
            if block_ms is not None and block_ms > 0:
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

    def request_cancel(self, run_id: str) -> bool:
        """Request cancellation of a run."""
        r = self._redis
        if r is None:
            return False
        # Compare and set atomically: cancellation must not overwrite a completed
        # run or release the session while its worker is still executing.
        return bool(
            r.eval(
                """
            local state = redis.call('GET', KEYS[1])
            if state == 'cancelling' then return 1 end
            if state ~= 'running' then return 0 end
            redis.call('SET', KEYS[2], '1', 'EX', ARGV[1])
            redis.call('SET', KEYS[1], 'cancelling', 'EX', ARGV[1])
            return 1
            """,
                2,
                f"chat:run:{run_id}:status",
                f"chat:run:{run_id}:cancel",
                _RUN_TTL,
            )
        )

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
