import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import encrypt_credentials, get_current_key_version
from app.models.connection import Connection


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
    """Stub: test a connection. Always returns success in Phase 1."""
    result = await db.execute(
        select(Connection).where(Connection.id == connection_id, Connection.tenant_id == tenant_id)
    )
    connection = result.scalar_one_or_none()
    if not connection:
        return {"connection_id": str(connection_id), "status": "error", "message": "Connection not found"}
    return {
        "connection_id": str(connection_id),
        "status": "ok",
        "message": f"Stub: {connection.provider} connection test passed",
    }
