"""Chat run endpoints — SSE stream relay and graceful cancel."""

from __future__ import annotations

import asyncio
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

    async def _generate():
        cursor = last_id
        queue: asyncio.Queue = asyncio.Queue()
        _SENTINEL = object()

        async def _reader():
            """Read Redis stream in a loop, put events into queue one-by-one."""
            nonlocal cursor
            try:
                while True:
                    events = await asyncio.to_thread(rm.read_events, run_id, cursor, 50, 1000)
                    if events:
                        for event in events:
                            cursor = event["id"]
                            await queue.put(event["data"])
                    else:
                        # No events — check if run is done
                        current = await asyncio.to_thread(rm.get_status, run_id)
                        if current in _TERMINAL_STATUSES:
                            # Drain any remaining events
                            remaining = await asyncio.to_thread(rm.read_events, run_id, cursor, 100, 0)
                            for event in remaining:
                                cursor = event["id"]
                                await queue.put(event["data"])
                            await queue.put({"type": "run_status", "status": current})
                            await queue.put(_SENTINEL)
                            return
                    # Check terminal after reading events too
                    current = await asyncio.to_thread(rm.get_status, run_id)
                    if current in _TERMINAL_STATUSES:
                        remaining = await asyncio.to_thread(rm.read_events, run_id, cursor, 100, 0)
                        for event in remaining:
                            cursor = event["id"]
                            await queue.put(event["data"])
                        await queue.put({"type": "run_status", "status": current})
                        await queue.put(_SENTINEL)
                        return
            except Exception:
                await queue.put(_SENTINEL)

        reader_task = asyncio.create_task(_reader())

        # 8KB padding for Cloudflare
        yield f": {' ' * 8192}\n\n"

        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue

                if item is _SENTINEL:
                    return

                yield f"data: {json.dumps(item)}\n\n"
        finally:
            reader_task.cancel()

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
