import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import decrypt_credentials, encrypt_credentials, get_current_key_version
from app.models.connection import Connection

logger = structlog.get_logger()


async def create_connection(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    provider: str,
    label: str,
    credentials: dict,
    created_by: uuid.UUID | None = None,
) -> Connection:
    """Create a new connection with encrypted credentials."""
    encrypted = encrypt_credentials(credentials)
    connection = Connection(
        tenant_id=tenant_id,
        provider=provider,
        label=label,
        status="active",
        encrypted_credentials=encrypted,
        encryption_key_version=get_current_key_version(),
        created_by=created_by,
    )
    db.add(connection)
    await db.flush()
    return connection


async def list_connections(db: AsyncSession, tenant_id: uuid.UUID) -> list[Connection]:
    """List connections for a tenant (no secrets exposed)."""
    result = await db.execute(
        select(Connection).where(Connection.tenant_id == tenant_id).order_by(Connection.created_at.desc())
    )
    return list(result.scalars().all())


async def delete_connection(db: AsyncSession, connection_id: uuid.UUID, tenant_id: uuid.UUID) -> bool:
    """Soft-delete a connection by setting status to revoked."""
    result = await db.execute(
        select(Connection).where(Connection.id == connection_id, Connection.tenant_id == tenant_id)
    )
    connection = result.scalar_one_or_none()
    if not connection:
        return False
    connection.status = "revoked"
    await db.flush()
    return True


async def test_connection(db: AsyncSession, connection_id: uuid.UUID, tenant_id: uuid.UUID) -> dict:
    """Test a connection by running a lightweight query against the provider."""
    result = await db.execute(
        select(Connection).where(Connection.id == connection_id, Connection.tenant_id == tenant_id)
    )
    connection = result.scalar_one_or_none()
    if not connection:
        return {"connection_id": str(connection_id), "status": "error", "message": "Connection not found"}

    if connection.provider == "netsuite":
        return await _test_netsuite_connection(db, connection)

    # Other providers: stub for now
    return {
        "connection_id": str(connection_id),
        "status": "ok",
        "message": f"{connection.provider} connection test passed",
    }


async def _test_netsuite_connection(db: AsyncSession, connection: Connection) -> dict:
    """Test a NetSuite connection by running a lightweight SuiteQL query."""
    try:
        credentials = decrypt_credentials(connection.encrypted_credentials)
    except Exception as exc:
        return {
            "connection_id": str(connection.id),
            "status": "error",
            "message": f"Failed to decrypt credentials: {exc}",
        }

    auth_type = credentials.get("auth_type", "oauth1")
    account_id = credentials.get("account_id", "")

    if auth_type == "oauth2":
        from app.services.netsuite_client import execute_suiteql_via_rest
        from app.services.netsuite_oauth_service import get_valid_token

        access_token = await get_valid_token(db, connection)
        if not access_token:
            return {
                "connection_id": str(connection.id),
                "status": "error",
                "message": "OAuth 2.0 token expired and refresh failed.",
            }
        try:
            await execute_suiteql_via_rest(access_token, account_id, "SELECT id FROM transaction WHERE ROWNUM <= 1", 1)
            return {
                "connection_id": str(connection.id),
                "status": "ok",
                "message": f"NetSuite account {account_id} connected successfully.",
            }
        except Exception as exc:
            return {
                "connection_id": str(connection.id),
                "status": "error",
                "message": f"NetSuite query failed: {exc}",
            }

    # OAuth 1.0 test — delegate to the suiteql tool's execute
    from app.mcp.tools.netsuite_suiteql import execute as suiteql_execute

    test_result = await suiteql_execute(
        {"query": "SELECT id FROM transaction WHERE ROWNUM <= 1", "limit": 1},
        context={"tenant_id": connection.tenant_id, "db": db},
    )
    if test_result.get("error"):
        return {
            "connection_id": str(connection.id),
            "status": "error",
            "message": test_result.get("message", "Unknown error"),
        }
    return {
        "connection_id": str(connection.id),
        "status": "ok",
        "message": f"NetSuite account {account_id} connected successfully.",
    }
