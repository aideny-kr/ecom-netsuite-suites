"""Chat run endpoints — SSE stream relay and graceful cancel."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.chat import ChatSession
from app.models.user import User
from app.services.chat.run_manager import get_run_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat/runs", tags=["chat-runs"])

_TERMINAL_STATUSES = {"complete", "cancelled", "failed"}


async def _require_run_owner(db: AsyncSession, run_id: str, user: User, rm) -> None:
    # A run ID is not an access capability. Legacy/unbound runs fail closed;
    # persisted conversation history remains available through its owned session.
    try:
        session_id = uuid.UUID(rm.get_session_id(run_id) or "")
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    result = await db.execute(
        select(ChatSession.id).where(
            ChatSession.id == session_id,
            ChatSession.tenant_id == user.tenant_id,
            ChatSession.user_id == user.id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")


@router.get("/{run_id}/stream")
async def stream_run(
    run_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    last_id: str = Query(default="0"),
):
    """SSE relay — reads events from the Redis Stream for a run."""
    rm = get_run_manager()

    if not rm.available:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis unavailable",
        )

    await _require_run_owner(db, run_id, user, rm)
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
                    # Check terminal even after an empty read. Drain every
                    # remaining page before status; reconnects may have a backlog.
                    current = await asyncio.to_thread(rm.get_status, run_id)
                    if current in _TERMINAL_STATUSES:
                        while True:
                            remaining = await asyncio.to_thread(rm.read_events, run_id, cursor, 100, None)
                            if not remaining:
                                break
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
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Request graceful cancellation of a running chat run."""
    rm = get_run_manager()

    if not rm.available:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis unavailable",
        )

    await _require_run_owner(db, run_id, user, rm)
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
