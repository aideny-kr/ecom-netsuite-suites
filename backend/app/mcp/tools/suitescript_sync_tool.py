"""MCP tool to trigger SuiteScript file sync from the chat interface.

Allows users to say "sync my suitescript files" in the chat and
have the system discover and load scripts from their NetSuite account.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.connection import Connection


async def execute(params: dict, context: dict | None = None, **kwargs) -> dict:
    """Trigger an async SuiteScript sync for the current tenant."""
    if not context:
        return {"error": True, "message": "Missing context — tenant_id required."}

    tenant_id = context.get("tenant_id")
    db = context.get("db")
    if not tenant_id or not db:
        return {"error": True, "message": "Missing tenant_id or db in context."}

    actor_id = context.get("actor_id")

    # Find active NetSuite connection
    result = await db.execute(
        select(Connection).where(
            Connection.tenant_id == tenant_id,
            Connection.provider == "netsuite",
            Connection.status == "active",
        )
    )
    connection = result.scalar_one_or_none()
    if not connection:
        return {
            "error": True,
            "message": (
                "No active NetSuite connection found. "
                "Please connect your NetSuite account first via Settings."
            ),
        }

    from app.workers.tasks.suitescript_sync import netsuite_suitescript_sync

    task = netsuite_suitescript_sync.delay(
        tenant_id=str(tenant_id),
        connection_id=str(connection.id),
        user_id=str(actor_id) if actor_id else None,
    )

    return {
        "status": "sync_queued",
        "task_id": task.id,
        "message": (
            "SuiteScript file sync has been queued. "
            "JavaScript files and custom scripts will be discovered via SuiteQL "
            "and loaded into the 'NetSuite Scripts' workspace. "
            "This typically takes 30-60 seconds depending on the number of files."
        ),
    }
