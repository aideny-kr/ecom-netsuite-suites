"""API endpoints for SuiteScript file sync from NetSuite."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.connection import Connection
from app.models.user import User

router = APIRouter(prefix="/netsuite/scripts", tags=["netsuite-scripts"])


@router.post("/sync")
async def trigger_script_sync(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Queue an async task to discover and load SuiteScript files from NetSuite.

    Finds the active NetSuite connection for the tenant, then queues a Celery
    task that discovers JS files via SuiteQL and fetches content via REST API.
    Files are stored in a dedicated 'NetSuite Scripts' workspace.
    """
    # Find active NetSuite connection
    result = await db.execute(
        select(Connection).where(
            Connection.tenant_id == user.tenant_id,
            Connection.provider == "netsuite",
            Connection.status == "active",
        )
    )
    connection = result.scalar_one_or_none()
    if not connection:
        raise HTTPException(
            status_code=400,
            detail="No active NetSuite connection found. Please connect your NetSuite account first.",
        )

    from app.workers.tasks.suitescript_sync import netsuite_suitescript_sync

    task = netsuite_suitescript_sync.delay(
        tenant_id=str(user.tenant_id),
        connection_id=str(connection.id),
        user_id=str(user.id),
    )
    return {"task_id": task.id, "status": "queued"}


@router.get("/sync-status")
async def get_sync_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the current SuiteScript sync state for the tenant."""
    from app.services.suitescript_sync_service import get_sync_status

    status = await get_sync_status(db, user.tenant_id)
    if status is None:
        return {"status": "not_started", "message": "SuiteScript sync has not been run yet."}

    return status
