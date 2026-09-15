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
from app.services import audit_service
from app.services.chat.run_manager import get_run_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat/runs", tags=["chat-runs"])

_TERMINAL_STATUSES = {"complete", "cancelled", "failed"}


async def _owned_run(run_id: str, user: User, db: AsyncSession):
    rm = get_run_manager()
    if not rm.available:
        raise HTTPException(503, "Redis unavailable")
    session_id = rm.get_session(run_id)
    try:
        session_uuid = uuid.UUID(session_id) if session_id else None
    except (ValueError, TypeError):
        session_uuid = None
    if session_uuid is None:
        raise HTTPException(404, "Run not found")
    owned = await db.scalar(
        select(ChatSession.id).where(
            ChatSession.id == session_uuid,
            ChatSession.tenant_id == user.tenant_id,
            ChatSession.user_id == user.id,
        )
    )
    if owned is None or rm.get_status(run_id) is None:
        raise HTTPException(404, "Run not found")
    return rm, str(owned)


@router.get("/{run_id}")
async def get_run_status(
    run_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Inspect lifecycle without opening an SSE connection. Missing/expired = 404."""
    rm, session_id = await _owned_run(run_id, user, db)
    run_status = rm.get_status(run_id)
    return {
        "run_id": run_id,
        "session_id": session_id,
        "status": run_status,
        "terminal": run_status in _TERMINAL_STATUSES,
        "started_at": rm.get_started_at(run_id),
        "outcome": rm.get_outcome(run_id),
    }


@router.get("/{run_id}/stream")
async def stream_run(
    run_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    last_id: str = Query(default="0", pattern=r"^\d+(?:-\d+)?$", max_length=40),
):
    """SSE relay — reads events from the Redis Stream for a run."""
    rm, _ = await _owned_run(run_id, user, db)

    # Ownership lookup must not hold a pooled DB connection for the SSE lifetime.
    await db.rollback()

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
                    current = await asyncio.to_thread(rm.get_status, run_id)
                    if current in _TERMINAL_STATUSES:
                        # Drain ALL remaining pages without Redis BLOCK 0 (which
                        # means wait forever). Reconnecting to a completed run may
                        # have thousands of events, including its final message.
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
                    if current is None:
                        await queue.put({"type": "error", "error": "Run events have expired. Reload the conversation."})
                        await queue.put(_SENTINEL)
                        return
            except Exception:
                await queue.put({"type": "error", "error": "Run stream unavailable. Reconnect to inspect progress."})
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
    rm, _ = await _owned_run(run_id, user, db)

    run_status = rm.get_status(run_id)
    if run_status is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )

    if run_status not in {"running", "cancelling"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run is not running (status: {run_status})",
        )

    if not rm.request_cancel(run_id):
        raise HTTPException(409, "Run is not running")
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="chat",
        action="chat.run.cancel",
        actor_id=user.id,
        resource_type="chat_run",
        resource_id=run_id,
    )
    await db.commit()
    return {"status": "cancelling", "run_id": run_id}
