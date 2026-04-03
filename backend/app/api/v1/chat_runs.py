"""Chat run endpoints — SSE stream relay and graceful cancel."""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.core.dependencies import get_current_user
from app.models.user import User
from app.services.chat.run_manager import get_run_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat/runs", tags=["chat-runs"])

_TERMINAL_STATUSES = {"complete", "cancelled", "failed"}


@router.get("/{run_id}/stream")
async def stream_run(
    run_id: str,
    user: Annotated[User, Depends(get_current_user)],
    last_id: str = Query(default="0"),
):
    """SSE relay — reads events from the Redis Stream for a run."""
    rm = get_run_manager()

    if not rm.available:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis unavailable",
        )

    run_status = rm.get_status(run_id)
    if run_status is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )

    def _generate():
        cursor = last_id

        # 8KB padding for Cloudflare
        yield f": {' ' * 8192}\n\n"

        while True:
            events = rm.read_events(
                run_id, last_id=cursor, count=100, block_ms=5000
            )

            if not events:
                # Check if run is terminal
                current = rm.get_status(run_id)
                if current in _TERMINAL_STATUSES:
                    yield f"data: {json.dumps({'type': 'run_status', 'status': current})}\n\n"
                    return
                # Heartbeat
                yield ": heartbeat\n\n"
                continue

            for event in events:
                cursor = event["id"]
                yield f"data: {json.dumps(event['data'])}\n\n"

            # After flushing events, check terminal
            current = rm.get_status(run_id)
            if current in _TERMINAL_STATUSES:
                # Drain any remaining events
                remaining = rm.read_events(run_id, last_id=cursor, count=100)
                for event in remaining:
                    cursor = event["id"]
                    yield f"data: {json.dumps(event['data'])}\n\n"
                yield f"data: {json.dumps({'type': 'run_status', 'status': current})}\n\n"
                return

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    user: Annotated[User, Depends(get_current_user)],
):
    """Request graceful cancellation of a running chat run."""
    rm = get_run_manager()

    if not rm.available:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis unavailable",
        )

    run_status = rm.get_status(run_id)
    if run_status is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )

    if run_status != "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run is not running (status: {run_status})",
        )

    rm.request_cancel(run_id)
    return {"status": "cancelling", "run_id": run_id}
