import asyncio
import re
import uuid
from datetime import datetime, timezone

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import decrypt_credentials, encrypt_credentials, get_current_key_version
from app.models.connection import RETIRED_CONNECTION_STATUSES, Connection
from app.services.celigo_write_guard import CeligoManagedElsewhereError
from app.services.http_connector_service import HTTP_PROVIDERS, public_metadata, validate_credentials, verify_connection

logger = structlog.get_logger()

__all__ = [
    "CeligoManagedElsewhereError",
    "create_connection",
    "delete_connection",
    "get_connection",
    "list_connections",
    "test_connection",
]


async def create_connection(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    provider: str,
    label: str,
    credentials: dict,
    created_by: uuid.UUID | None = None,
) -> Connection:
    """Create a new connection with encrypted credentials."""
    if provider in HTTP_PROVIDERS:
        credentials = validate_credentials(provider, credentials)
    encrypted = encrypt_credentials(credentials)
    connection = Connection(
        tenant_id=tenant_id,
        provider=provider,
        label=label,
        status="pending" if provider in HTTP_PROVIDERS else "active",
        auth_type=credentials["auth_type"] if provider in HTTP_PROVIDERS else "oauth2",
        metadata_json=public_metadata(credentials) if provider in HTTP_PROVIDERS else None,
        encrypted_credentials=encrypted,
        encryption_key_version=get_current_key_version(),
        created_by=created_by,
    )
    db.add(connection)
    await db.flush()
    return connection


async def get_connection(db: AsyncSession, connection_id: uuid.UUID, tenant_id: uuid.UUID) -> Connection | None:
    """Get a single connection by ID."""
    result = await db.execute(
        select(Connection).where(Connection.id == connection_id, Connection.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()


async def list_connections(db: AsyncSession, tenant_id: uuid.UUID) -> list[Connection]:
    """List connections for a tenant (no secrets exposed)."""
    result = await db.execute(
        select(Connection)
        .where(Connection.tenant_id == tenant_id, Connection.status != "revoked")
        .order_by(Connection.created_at.desc())
    )
    return list(result.scalars().all())


# CeligoManagedElsewhereError is imported at the top of this module and
# re-exported here (see __all__ below) rather than defined. The class moved to
# celigo_write_guard because the session-flush listener raises it too, and that
# module cannot import this one -- it is imported BY app.models.connection,
# which this module imports. Callers that already catch
# connection_service.CeligoManagedElsewhereError keep working unchanged, and
# now catch the guard's refusals as the same type.


async def delete_connection(db: AsyncSession, connection_id: uuid.UUID, tenant_id: uuid.UUID) -> bool:
    """Soft-delete a connection by setting status to revoked.

    Refuses celigo rows: this endpoint is provider-agnostic and has no idea a
    Celigo connection has a paired celigo_mcp McpConnector (created by
    connector_status._upsert_celigo_mcp_connector) giving the chat agent
    Celigo tools. Revoking the Connection row here alone would leave that
    connector live -- the user believes they disconnected, but the agent
    keeps its Celigo tools. disconnect_celigo (DELETE /connector-status/celigo)
    revokes both rows together; callers must go through it instead.
    """
    result = await db.execute(
        select(Connection).where(Connection.id == connection_id, Connection.tenant_id == tenant_id)
    )
    connection = result.scalar_one_or_none()
    if not connection:
        return False
    if connection.provider == "celigo":
        raise CeligoManagedElsewhereError(
            "Celigo connections must be disconnected via DELETE /connector-status/celigo, "
            "which also revokes the paired celigo_mcp connector."
        )
    connection.status = "revoked"
    await db.flush()
    return True


async def test_connection(db: AsyncSession, connection_id: uuid.UUID, tenant_id: uuid.UUID) -> dict:
    """Test a connection by running a lightweight query against the provider."""
    result = await db.execute(
        select(Connection).where(Connection.id == connection_id, Connection.tenant_id == tenant_id)
    )
    connection = result.scalar_one_or_none()
    if not connection or connection.status in RETIRED_CONNECTION_STATUSES:
        return {"connection_id": str(connection_id), "status": "error", "message": "Connection not found"}

    if connection.provider in HTTP_PROVIDERS:
        return await verify_connection(db, connection)

    if connection.provider in ("netsuite", "stripe"):
        from app.services.connection_verification import check_stripe, failure_message

        try:
            async with asyncio.timeout(30):
                if connection.provider == "netsuite":
                    outcome = await _test_netsuite_connection(db, connection)
                else:
                    outcome = await check_stripe(decrypt_credentials(connection.encrypted_credentials))
        except Exception as exc:
            outcome = {"status": "error", "message": failure_message(connection.provider.title(), exc)}
        connection.last_health_check_at = datetime.now(timezone.utc)
        connection.status = "active" if outcome["status"] == "ok" else "error"
        connection.error_reason = None if outcome["status"] == "ok" else outcome["message"]
        await db.flush()
        return {"connection_id": str(connection_id), **outcome}

    return {
        "connection_id": str(connection_id),
        "status": "unsupported",
        "message": "Use this provider's dedicated setup to verify read access",
    }


async def _test_netsuite_connection(db: AsyncSession, connection: Connection) -> dict:
    """Verify the selected account's SuiteQL read access, independently of File Cabinet setup."""
    from app.services.http_connector_service import ConnectorReadError

    credentials = decrypt_credentials(connection.encrypted_credentials)
    account_id = credentials.get("account_id", "")
    if not isinstance(account_id, str) or not re.fullmatch(r"[0-9]+(?:[-_](?:SB[0-9]+|RP))?", account_id, re.I):
        raise ValueError("Invalid account")
    query = "SELECT id FROM transaction WHERE ROWNUM <= 1"
    try:
        if credentials.get("auth_type", "oauth1") == "oauth2":
            from app.services.netsuite_client import execute_suiteql_via_rest
            from app.services.netsuite_oauth_service import get_valid_token

            access_token = await get_valid_token(db, connection)
            if not access_token:
                raise ConnectorReadError("authentication_failed")
            await execute_suiteql_via_rest(access_token, account_id, query, 1, timeout_seconds=20)
        else:
            from app.mcp.tools.netsuite_suiteql import build_oauth1_header

            slug = account_id.replace("_", "-").lower()
            url = f"https://{slug}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"
            headers = {**build_oauth1_header(credentials, "POST", url), "Prefer": "transient"}
            async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
                response = await client.post(url, headers=headers, json={"q": query})
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                    raise ConnectorReadError("invalid_response")
    except httpx.HTTPStatusError as exc:
        code = (
            "authentication_failed"
            if exc.response.status_code in (401, 403)
            else ("rate_limited" if exc.response.status_code == 429 else "http_error")
        )
        raise ConnectorReadError(code) from None
    return {"connection_id": str(connection.id), "status": "ok", "message": "NetSuite SuiteQL read access verified."}
