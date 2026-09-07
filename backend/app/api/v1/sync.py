import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.models.user import User
from app.services import audit_service
from app.services.ingestion.sync_status import solidus_sync_status

router = APIRouter(prefix="/connections", tags=["sync"])

SYNC_TASK_MAP = {
    "stripe": "tasks.stripe_sync",
    "shopify": "tasks.shopify_sync",
    "solidus": "tasks.solidus_sync",
}


@router.post("/{connection_id}/sync")
async def trigger_sync(
    connection_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Trigger a data sync for a connection."""
    # Validate connection exists and belongs to tenant
    result = await db.execute(
        select(Connection)
        .where(
            Connection.id == connection_id,
            Connection.tenant_id == user.tenant_id,
        )
        .with_for_update()
    )
    connection = result.scalar_one_or_none()
    if not connection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")

    if connection.status not in ACTIVE_CONNECTION_STATUSES:
        raise HTTPException(status_code=409, detail="Test or reconnect this connection before refreshing data.")

    if connection.provider == "solidus":
        return await _trigger_solidus(db, user, connection)

    if connection.provider not in SYNC_TASK_MAP:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Sync not supported for provider: {connection.provider}",
        )

    # Dispatch Celery task
    from app.workers.celery_app import celery_app

    task_name = SYNC_TASK_MAP[connection.provider]
    result = celery_app.send_task(
        task_name,
        kwargs={
            "tenant_id": str(user.tenant_id),
            "connection_id": str(connection_id),
        },
        queue="sync",
    )

    # Audit
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="sync",
        action="sync.trigger",
        actor_id=user.id,
        actor_type="user",
        resource_type="connection",
        resource_id=str(connection_id),
        payload={"provider": connection.provider, "task_id": result.id},
    )
    await db.commit()

    return {
        "job_id": result.id,
        "status": "queued",
        "message": f"Sync triggered for {connection.provider} connection",
    }


async def _trigger_solidus(db, user, connection):
    from kombu.exceptions import OperationalError

    from app.workers.celery_app import celery_app

    if (connection.metadata_json or {}).get("api_profile") != "framework_sync":
        raise HTTPException(status_code=422, detail="Order import requires the Framework Sync API profile.")
    current = await solidus_sync_status(db, user.tenant_id, connection.id)
    if current["status"] in {"queued", "running"}:
        return {"job_id": current["job_id"], "status": current["status"], "already_running": True}
    task_id = str(uuid.uuid4())
    tenant_id, connection_id, actor_id = user.tenant_id, connection.id, user.id
    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="sync",
        action="sync.trigger",
        actor_id=actor_id,
        resource_type="connection",
        resource_id=str(connection_id),
        payload={"provider": "solidus", "task_id": task_id, "requested_at": datetime.now(timezone.utc).isoformat()},
    )
    # Persist the request before queueing. Concurrent clicks see the reservation;
    # a fast worker never sees an uncommitted connection-row lock.
    await db.commit()
    try:
        await run_in_threadpool(
            celery_app.send_task,
            "tasks.solidus_sync",
            task_id=task_id,
            queue="sync",
            kwargs={
                "tenant_id": str(tenant_id),
                "connection_id": str(connection_id),
                "refresh_request_id": task_id,
                "correlation_id": task_id,
            },
        )
    except (OperationalError, OSError):
        from app.core.database import set_tenant_context

        await set_tenant_context(db, tenant_id)
        await audit_service.log_event(
            db=db,
            tenant_id=tenant_id,
            category="sync",
            action="sync.trigger_failed",
            actor_id=actor_id,
            resource_type="connection",
            resource_id=str(connection_id),
            status="error",
            payload={"provider": "solidus", "task_id": task_id, "requested_at": datetime.now(timezone.utc).isoformat()},
        )
        await db.commit()
        raise HTTPException(status_code=503, detail="The refresh queue is unavailable. Please retry.") from None
    return {"job_id": task_id, "status": "queued", "already_running": False}


@router.get("/{connection_id}/sync-status")
async def sync_status(
    connection_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    connection = await db.scalar(
        select(Connection).where(
            Connection.id == connection_id,
            Connection.tenant_id == user.tenant_id,
        )
    )
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    if connection.provider != "solidus":
        raise HTTPException(status_code=422, detail="Order refresh status is available for Solidus connections.")
    return await solidus_sync_status(db, user.tenant_id, connection.id)
