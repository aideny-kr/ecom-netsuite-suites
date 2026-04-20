"""Writes progress events to Redis (for SSE subscribers) AND persists
incremental progress to Postgres so GET /runs/{id} snapshots stay fresh
mid-run (if the user refreshes the page while a run is in progress).

Runs inside the sync Celery worker process. For the async SSE endpoint
side, see backend/app/api/v1/agent_lab.py::stream_events.
"""

from __future__ import annotations

import json
from uuid import UUID

import redis
from sqlalchemy.orm import Session

from app.models.agent_lab_run import AgentLabRun


class ProgressEmitter:
    def __init__(self, run_id: UUID, redis_client: redis.Redis, sync_db: Session):
        self._run_id = run_id
        self._redis = redis_client
        self._db = sync_db
        self._stream = f"agent_lab_run:{run_id}"
        self._cancel = f"agent_lab_run:{run_id}:cancel"
        self._initialized = False

    def emit(self, event: str, payload: dict) -> None:
        # 1. Publish to Redis stream for SSE subscribers
        self._redis.xadd(
            self._stream,
            {"event": event, "data": json.dumps(payload)},
            maxlen=1000,
            approximate=True,
        )
        # Stream TTL set once on first emit. MAXLEN caps entries count;
        # EXPIRE caps wall-clock age of the whole key.
        if not self._initialized:
            self._redis.expire(self._stream, 1800)  # 30 min
            self._initialized = True

        # 2. On case_complete, persist incremental progress so refreshed
        # /runs/{id} requests don't show stale 0/18 during in-flight runs.
        if event == "case_complete":
            self._db.query(AgentLabRun).filter_by(id=self._run_id).update({
                "cases_completed": payload["cases_completed"],
                "cost_usd_actual": payload["running_cost_usd"],
            })
            self._db.commit()

    def cancelled(self) -> bool:
        return bool(self._redis.get(self._cancel))
