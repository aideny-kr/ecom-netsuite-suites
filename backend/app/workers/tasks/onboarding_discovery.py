"""Celery task for tenant onboarding deep discovery."""

import asyncio

from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app


@celery_app.task(
    base=InstrumentedTask,
    bind=True,
    name="tasks.onboarding_discovery",
    queue="sync",
    soft_time_limit=300,
    time_limit=420,
)
def onboarding_discovery_task(self, tenant_id: str, connection_id: str):
    """Run deep tenant discovery on first NetSuite connection."""
    from app.core.database import async_session_factory
    from app.core.encryption import decrypt_credentials
    from app.models.connection import Connection
    from app.services.netsuite_oauth_service import get_valid_token
    from app.services.knowledge.onboarding_discovery import run_onboarding_discovery
    from sqlalchemy import select
    import uuid

    loop = asyncio.new_event_loop()
    try:

        async def _run():
            async with async_session_factory() as db:
                # Get connection and token
                result = await db.execute(
                    select(Connection).where(
                        Connection.id == uuid.UUID(connection_id),
                        Connection.tenant_id == uuid.UUID(tenant_id),
                    )
                )
                connection = result.scalar_one_or_none()
                if not connection:
                    return {"error": "Connection not found"}

                access_token = await get_valid_token(db, connection)
                if not access_token:
                    return {"error": "Failed to get valid token"}

                creds = decrypt_credentials(connection.encrypted_credentials)
                account_id = (creds.get("account_id") or "").replace("_", "-").lower()

                discovery_result = await run_onboarding_discovery(
                    db=db,
                    tenant_id=uuid.UUID(tenant_id),
                    access_token=access_token,
                    account_id=account_id,
                )

                return {
                    "phases": [
                        {
                            "name": p.name,
                            "success": p.success,
                            "duration_ms": p.duration_ms,
                            "error": p.error,
                        }
                        for p in discovery_result.phases
                    ],
                    "total_duration_ms": discovery_result.total_duration_ms,
                    "queries_executed": discovery_result.queries_executed,
                    "message": (
                        f"Discovery complete: {discovery_result.queries_executed} queries "
                        f"in {discovery_result.total_duration_ms}ms"
                    ),
                }

        return loop.run_until_complete(_run())
    finally:
        loop.close()
