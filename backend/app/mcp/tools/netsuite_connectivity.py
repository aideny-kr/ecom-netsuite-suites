"""NetSuite connectivity check tool."""

from __future__ import annotations

from sqlalchemy import select

from app.core.encryption import decrypt_credentials
from app.models.connection import Connection
from app.mcp.tools.netsuite_suiteql import execute as suiteql_execute


async def execute_connectivity(
    params: dict, context: dict | None = None, **kwargs
) -> dict:
    """Test NetSuite connectivity by running a lightweight health query."""
    if not context:
        return {
            "status": "error",
            "message": "Missing context — tenant_id and db session are required.",
        }

    tenant_id = context.get("tenant_id")
    db = context.get("db")
    if not tenant_id or not db:
        return {
            "status": "error",
            "message": "Context must include tenant_id and db.",
        }

    # Look up connection to get account_id for the response
    try:
        result = await db.execute(
            select(Connection).where(
                Connection.tenant_id == tenant_id,
                Connection.provider == "netsuite",
                Connection.status == "active",
            )
        )
        connection = result.scalars().first()
        if not connection:
            return {
                "status": "error",
                "message": "No active NetSuite connection found for this tenant.",
            }
        credentials = decrypt_credentials(connection.encrypted_credentials)
        account_id = credentials.get("account_id", "unknown")
    except Exception as exc:
        return {"status": "error", "message": f"Connection lookup failed: {exc}"}

    # Delegate to suiteql execute with a lightweight health query
    health_result = await suiteql_execute(
        {"query": "SELECT 1 AS health", "limit": 1},
        context=context,
    )

    if health_result.get("error"):
        return {
            "status": "error",
            "message": health_result.get("message", "Unknown error"),
        }

    return {
        "status": "ok",
        "account_id": account_id,
        "message": f"Successfully connected to NetSuite account {account_id}.",
    }
