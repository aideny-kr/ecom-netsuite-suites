"""Celery task for async SuiteScript file sync from NetSuite.

Runs on the 'sync' queue. Discovers JavaScript files and custom scripts
from the tenant's NetSuite account, fetches content, and loads into workspace.
"""

import asyncio
import uuid

from sqlalchemy import select

from app.core.database import set_tenant_context, worker_async_session
from app.core.encryption import decrypt_credentials
from app.models.connection import Connection
from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app


@celery_app.task(
    base=InstrumentedTask,
    bind=True,
    name="tasks.netsuite_suitescript_sync",
    queue="sync",
    soft_time_limit=300,
    time_limit=420,
)
def netsuite_suitescript_sync(
    self,
    tenant_id: str,
    connection_id: str,
    user_id: str | None = None,
    **kwargs,
):
    """Discover and load SuiteScript files from NetSuite into workspace."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_execute(tenant_id, connection_id, user_id))
    finally:
        loop.close()


async def _execute(
    tenant_id: str,
    connection_id: str,
    user_id: str | None,
) -> dict:
    """Async inner: open session, decrypt credentials, run sync."""
    from app.services.suitescript_sync_service import sync_scripts_to_workspace

    async with worker_async_session() as session:
        await set_tenant_context(session, tenant_id)

        # Get connection and decrypt OAuth credentials
        result = await session.execute(
            select(Connection).where(
                Connection.id == uuid.UUID(connection_id),
                Connection.tenant_id == uuid.UUID(tenant_id),
                Connection.provider == "netsuite",
                Connection.status == "active",
            )
        )
        connection = result.scalar_one_or_none()
        if not connection:
            raise ValueError(f"Active NetSuite connection {connection_id} not found for tenant {tenant_id}")

        creds = decrypt_credentials(connection.encrypted_credentials)
        account_id = creds.get("account_id", "") or creds.get("netsuite_account_id", "")

        if not account_id:
            raise ValueError("Connection credentials missing account_id")

        # Auto-refresh token if expired (matches pattern used by SuiteQL tool and metadata discovery)
        from app.services.netsuite_oauth_service import get_valid_token

        access_token = await get_valid_token(session, connection)
        if not access_token:
            raise ValueError("OAuth 2.0 token expired and refresh failed. User must re-authorize.")

        sync_result = await sync_scripts_to_workspace(
            db=session,
            tenant_id=uuid.UUID(tenant_id),
            connection_id=uuid.UUID(connection_id),
            access_token=access_token,
            account_id=account_id,
            user_id=uuid.UUID(user_id) if user_id else None,
        )
        await session.commit()
        return sync_result
