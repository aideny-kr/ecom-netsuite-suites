import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.models.connection import Connection
from app.models.user import User
from app.services import audit_service

router = APIRouter(prefix="/connections", tags=["sync"])

SYNC_TASK_MAP = {
    "stripe": "tasks.stripe_sync",
    "shopify": "tasks.shopify_sync",
}


@router.post("/{connection_id}/sync")
async def trigger_sync(
    connection_id: uuid.UUID,
    user: User = Depends(require_permission("connections.manage")),
    db: AsyncSession = Depends(get_db),
):
    """Trigger a data sync for a connection."""
    # Validate connection exists and belongs to tenant
    result = await db.execute(
        select(Connection).where(
            Connection.id == connection_id,
            Connection.tenant_id == user.tenant_id,
        )
    )
    connection = result.scalar_one_or_none()
    if not connection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")

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
