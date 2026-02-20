import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import encrypt_credentials, get_current_key_version
from app.models.mcp_connector import McpConnector

logger = structlog.get_logger()


async def create_mcp_connector(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    provider: str,
    label: str,
    server_url: str,
    auth_type: str = "none",
    credentials: dict | None = None,
    created_by: uuid.UUID | None = None,
) -> McpConnector:
    """Create a new MCP connector with optional encrypted credentials."""
    encrypted = encrypt_credentials(credentials) if credentials else None
    connector = McpConnector(
        tenant_id=tenant_id,
        provider=provider,
        label=label,
        server_url=server_url,
        auth_type=auth_type,
        encrypted_credentials=encrypted,
        encryption_key_version=get_current_key_version() if encrypted else 1,
        status="active",
        is_enabled=True,
        created_by=created_by,
    )
    db.add(connector)
    await db.flush()
    return connector


def _netsuite_mcp_server_url(account_id: str) -> str:
    """Construct the NetSuite MCP server URL from account ID."""
    slug = account_id.replace("_", "-").lower()
    return f"https://{slug}.suitetalk.api.netsuite.com/services/mcp/v1/all"


async def create_netsuite_mcp_connector(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    account_id: str,
    client_id: str,
    token_data: dict,
    label: str | None = None,
    created_by: uuid.UUID | None = None,
) -> McpConnector:
    """Create an MCP connector for NetSuite using OAuth 2.0 tokens."""
    import time

    credentials = {
        "auth_type": "oauth2",
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_at": time.time() + int(token_data.get("expires_in", 3600)),
        "account_id": account_id,
        "client_id": client_id,
    }

    connector = await create_mcp_connector(
        db=db,
        tenant_id=tenant_id,
        provider="netsuite_mcp",
        label=label or f"NetSuite MCP {account_id}",
        server_url=_netsuite_mcp_server_url(account_id),
        auth_type="oauth2",
        credentials=credentials,
        created_by=created_by,
    )

    # Store account_id and client_id in metadata for reference
    connector.metadata_json = {"account_id": account_id, "client_id": client_id}
    await db.flush()

    return connector


async def update_connector_tokens(
    db: AsyncSession,
    connector: McpConnector,
    token_data: dict,
    account_id: str,
    client_id: str,
) -> None:
    """Update an existing connector's OAuth 2.0 tokens after re-authorization."""
    import time

    credentials = {
        "auth_type": "oauth2",
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_at": time.time() + int(token_data.get("expires_in", 3600)),
        "account_id": account_id,
        "client_id": client_id,
    }
    connector.encrypted_credentials = encrypt_credentials(credentials)
    connector.status = "active"
    await db.flush()


async def list_mcp_connectors(db: AsyncSession, tenant_id: uuid.UUID) -> list[McpConnector]:
    """List all MCP connectors for a tenant."""
    result = await db.execute(
        select(McpConnector).where(McpConnector.tenant_id == tenant_id).order_by(McpConnector.created_at.desc())
    )
    return list(result.scalars().all())


async def get_mcp_connector(db: AsyncSession, connector_id: uuid.UUID, tenant_id: uuid.UUID) -> McpConnector | None:
    """Get a single MCP connector by ID, scoped to tenant."""
    result = await db.execute(
        select(McpConnector).where(
            McpConnector.id == connector_id,
            McpConnector.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


async def delete_mcp_connector(db: AsyncSession, connector_id: uuid.UUID, tenant_id: uuid.UUID) -> bool:
    """Soft-delete an MCP connector by setting status to revoked."""
    connector = await get_mcp_connector(db, connector_id, tenant_id)
    if not connector:
        return False
    connector.status = "revoked"
    connector.is_enabled = False
    await db.flush()
    return True


async def get_active_connectors_for_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> list[McpConnector]:
    """Get all active and enabled MCP connectors for a tenant."""
    result = await db.execute(
        select(McpConnector).where(
            McpConnector.tenant_id == tenant_id,
            McpConnector.is_enabled.is_(True),
            McpConnector.status == "active",
        )
    )
    return list(result.scalars().all())


async def test_mcp_connector(db: AsyncSession, connector_id: uuid.UUID, tenant_id: uuid.UUID) -> dict:
    """Test an MCP connector by connecting and discovering tools."""
    connector = await get_mcp_connector(db, connector_id, tenant_id)
    if not connector:
        return {
            "connector_id": str(connector_id),
            "status": "error",
            "message": "Connector not found",
        }

    try:
        from app.services.mcp_client_service import discover_tools

        tools = await discover_tools(connector, db)
        connector.discovered_tools = tools
        await db.flush()

        return {
            "connector_id": str(connector.id),
            "status": "ok",
            "message": f"Connected successfully. Discovered {len(tools)} tools.",
            "discovered_tools": tools,
        }
    except Exception as exc:
        logger.warning(
            "mcp_connector.test_failed",
            connector_id=str(connector_id),
            error=str(exc),
        )
        return {
            "connector_id": str(connector.id),
            "status": "error",
            "message": f"Connection test failed: {exc}",
        }
