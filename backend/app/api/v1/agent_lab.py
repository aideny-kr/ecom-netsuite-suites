"""Agent Lab API — super-admin endpoints for on-demand benchmark + experiment runs.

All endpoints read tenant_id from settings.AGENT_BENCHMARK_TENANT_ID —
v1 is Framework-only. v1.1 multi-tenant work will replace this with a
query/path param.
"""

from __future__ import annotations

import asyncio
import json as _json
import time
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_superadmin
from app.core.redis_client import get_async_redis, get_sync_redis
from app.models.user import User
from app.services import audit_service
from app.services.agent_lab import service
from app.services.agent_lab.service import ConcurrentRunError

router = APIRouter(prefix="/agent-lab", tags=["agent-lab"])


# --------- Request / response models ---------


class CreateRunRequest(BaseModel):
    kind: Literal["benchmark", "experiment"]
    mode: Literal["all", "single"]
    case_id: str | None = None


class CreateRunResponse(BaseModel):
    run_id: str
    status: str


# --------- Endpoints ---------


@router.post(
    "/runs", response_model=CreateRunResponse, status_code=status.HTTP_201_CREATED
)
async def create_run(
    request: CreateRunRequest,
    user: Annotated[User, Depends(get_current_superadmin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if request.mode == "single" and not request.case_id:
        raise HTTPException(
            status_code=400, detail="case_id required when mode='single'"
        )

    tenant_id = _active_tenant_id()
    try:
        run = await service.start_run(
            db=db,
            user=user,
            tenant_id=tenant_id,
            kind=request.kind,
            mode=request.mode,
            case_id=request.case_id,
        )
    except ConcurrentRunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="agent_lab",
        action="agent_lab.run.start",
        actor_id=user.id,
        resource_type="agent_lab_run",
        resource_id=str(run.id),
    )
    await db.commit()

    return CreateRunResponse(run_id=str(run.id), status=run.status)


@router.get("/runs")
async def list_runs(
    user: Annotated[User, Depends(get_current_superadmin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    kind: str | None = Query(None),
    days: int = Query(14, ge=1, le=90),
):
    tenant_id = _active_tenant_id()
    runs = await service.list_runs(db, tenant_id=tenant_id, kind=kind, days=days)
    return [service._run_to_dict(r) for r in runs]


@router.get("/runs/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_superadmin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    snapshot = await service.get_run_snapshot(db, run_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="run not found")
    return snapshot


@router.post("/runs/{run_id}/cancel")
async def cancel_run_endpoint(
    run_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_superadmin)],
):
    # Idempotent: OK if already cancelled/completed
    r = get_sync_redis()
    service.cancel_run(r, run_id)
    return {"cancelled": True}


@router.get("/patterns")
async def list_patterns_endpoint(
    user: Annotated[User, Depends(get_current_superadmin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    tenant_id = _active_tenant_id()
    patterns = await service.list_patterns(db, tenant_id)
    return [
        {
            "id": str(p.id),
            "user_question": p.user_question,
            "working_sql": p.working_sql,
            "tables_used": p.tables_used,
            "success_count": p.success_count,
            "last_used_at": p.last_used_at.isoformat() if p.last_used_at else None,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in patterns
    ]


@router.get("/runs/{run_id}/events")
async def stream_events(
    run_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_superadmin)],
    last_id: str = Query("0-0"),
):
    """Server-Sent Events stream for a run. Reads from Redis stream
    agent_lab_run:{run_id}, relays to the browser. Injects heartbeat
    every 10s. Closes on run_complete event.

    Auth via HttpOnly JWT cookie — EventSource cannot set Authorization
    headers. Same mechanism as chat SSE.
    """
    stream_key = f"agent_lab_run:{run_id}"
    r = get_async_redis()

    async def generator():
        last = last_id.encode() if isinstance(last_id, str) else last_id
        last_heartbeat = time.monotonic()
        done = False

        while not done:
            try:
                response = await r.xread({stream_key: last}, block=5000, count=50)
            except Exception as exc:
                yield f"event: error\ndata: {_json.dumps({'error': str(exc)})}\n\n"
                return

            now = time.monotonic()
            if response:
                for _key, entries in response:
                    for entry_id, fields in entries:
                        last = entry_id
                        event_name = fields.get(b"event", b"message").decode()
                        data = fields.get(b"data", b"{}").decode()
                        yield f"id: {entry_id.decode()}\nevent: {event_name}\ndata: {data}\n\n"
                        if event_name == "run_complete":
                            done = True
                            break
                    if done:
                        break

            if now - last_heartbeat >= 10.0:
                yield f"event: heartbeat\ndata: {_json.dumps({'ts': int(now)})}\n\n"
                last_heartbeat = now

            if not response:
                await asyncio.sleep(0.1)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx hint: disable proxy buffering for SSE
        },
    )


# --------- Helpers ---------


def _active_tenant_id() -> uuid.UUID:
    raw = getattr(settings, "AGENT_BENCHMARK_TENANT_ID", None)
    if not raw:
        raise HTTPException(
            status_code=500,
            detail="AGENT_BENCHMARK_TENANT_ID not configured",
        )
    return uuid.UUID(raw)
