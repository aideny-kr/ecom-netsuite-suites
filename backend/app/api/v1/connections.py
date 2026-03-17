import time
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.core.encryption import decrypt_credentials, encrypt_credentials
from app.models.connection import Connection
from app.models.mcp_connector import McpConnector
from app.models.user import User
from app.schemas.connection import (
    ConnectionCreate,
    ConnectionResponse,
    ConnectionTestResponse,
    ConnectionUpdate,
)
from app.services import audit_service, connection_service, entitlement_service

router = APIRouter(prefix="/connections", tags=["connections"])


# ---------------------------------------------------------------------------
# Pydantic models for new endpoints
# ---------------------------------------------------------------------------


class ConnectionHealthItem(BaseModel):
    id: str
    label: str
    provider: str
    status: str
    auth_type: str | None = None
    token_expired: bool = False
    last_health_check: str | None = None
    tool_count: int | None = None  # MCP only
    client_id: str | None = None  # OAuth Client ID (public, not secret)
    restlet_url: str | None = None  # RESTlet URL (OAuth connections only)


class ConnectionHealthResponse(BaseModel):
    connections: list[ConnectionHealthItem]
    mcp_connectors: list[ConnectionHealthItem]


class ClientIdUpdate(BaseModel):
    client_id: str = Field(min_length=1)


class RestletUrlUpdate(BaseModel):
    restlet_url: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Health check — MUST be before /{connection_id} routes
# ---------------------------------------------------------------------------


@router.get("/health", response_model=ConnectionHealthResponse)
async def check_connection_health(
    user: Annotated[User, Depends(require_permission("connections.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Check health of all OAuth connections and MCP connectors for the tenant."""
    # Check OAuth connections
    result = await db.execute(
        select(Connection)
        .where(Connection.tenant_id == user.tenant_id, Connection.status != "revoked")
        .order_by(Connection.created_at.desc())
    )
    connections = result.scalars().all()

    conn_items = []
    now = time.time()
    for conn in connections:
        token_expired = False
        client_id = None
        if conn.auth_type == "oauth2" and conn.encrypted_credentials:
            try:
                creds = decrypt_credentials(conn.encrypted_credentials)
                expires_at = creds.get("expires_at", 0)
                client_id = creds.get("client_id")
                if expires_at and now > expires_at:
                    # Access token expired — this is normal (1hr lifetime).
                    # Don't flip status — get_valid_token() auto-refreshes on next use.
                    token_expired = True
            except Exception:
                pass
        restlet_url = (conn.metadata_json or {}).get("restlet_url") if conn.metadata_json else None
        conn_items.append(ConnectionHealthItem(
            id=str(conn.id),
            label=conn.label or conn.provider,
            provider=conn.provider,
            status=conn.status,
            auth_type=conn.auth_type,
            token_expired=token_expired,
            last_health_check=datetime.now(timezone.utc).isoformat(),
            client_id=client_id,
            restlet_url=restlet_url,
        ))

    # Check MCP connectors
    mcp_result = await db.execute(
        select(McpConnector)
        .where(McpConnector.tenant_id == user.tenant_id, McpConnector.status != "revoked")
        .order_by(McpConnector.created_at.desc())
    )
    mcp_connectors = mcp_result.scalars().all()

    mcp_items = []
    for mcp in mcp_connectors:
        token_expired = False
        mcp_client_id = (mcp.metadata_json or {}).get("client_id")
        if mcp.auth_type == "oauth2" and mcp.encrypted_credentials:
            try:
                creds = decrypt_credentials(mcp.encrypted_credentials)
                expires_at = creds.get("expires_at", 0)
                if not mcp_client_id:
                    mcp_client_id = creds.get("client_id")
                if expires_at and now > expires_at:
                    # Access token expired but MCP may still work via internal refresh
                    # Don't flip status — only mark token_expired for UI indicator
                    token_expired = True
            except Exception:
                pass
        tools = mcp.discovered_tools or []
        mcp_items.append(ConnectionHealthItem(
            id=str(mcp.id),
            label=mcp.label or mcp.provider,
            provider=mcp.provider,
            status=mcp.status,
            auth_type=mcp.auth_type,
            token_expired=token_expired,
            last_health_check=datetime.now(timezone.utc).isoformat(),
            client_id=mcp_client_id,
            tool_count=len(tools),
        ))

    # Read-only endpoint — do NOT commit (prevents accidental status corruption)
    return ConnectionHealthResponse(connections=conn_items, mcp_connectors=mcp_items)


@router.get("", response_model=list[ConnectionResponse])
async def list_connections(
    user: Annotated[User, Depends(require_permission("connections.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    connections = await connection_service.list_connections(db, user.tenant_id)
    return [
        ConnectionResponse(
            id=str(c.id),
            tenant_id=str(c.tenant_id),
            provider=c.provider,
            label=c.label,
            status=c.status,
            auth_type=c.auth_type,
            encryption_key_version=c.encryption_key_version,
            metadata_json=c.metadata_json,
            created_at=c.created_at,
            created_by=str(c.created_by) if c.created_by else None,
        )
        for c in connections
    ]


@router.post("", response_model=ConnectionResponse, status_code=status.HTTP_201_CREATED)
async def create_connection(
    request: ConnectionCreate,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    # Check entitlement
    allowed = await entitlement_service.check_entitlement(db, user.tenant_id, "connections")
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Connection limit reached for your plan",
        )

    connection = await connection_service.create_connection(
        db=db,
        tenant_id=user.tenant_id,
        provider=request.provider,
        label=request.label,
        credentials=request.credentials,
        created_by=user.id,
    )

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="connection",
        action="connection.create",
        actor_id=user.id,
        resource_type="connection",
        resource_id=str(connection.id),
        payload={"provider": request.provider, "label": request.label},
    )
    await db.commit()
    await db.refresh(connection)

    return ConnectionResponse(
        id=str(connection.id),
        tenant_id=str(connection.tenant_id),
        provider=connection.provider,
        label=connection.label,
        status=connection.status,
        auth_type=connection.auth_type,
        encryption_key_version=connection.encryption_key_version,
        metadata_json=connection.metadata_json,
        created_at=connection.created_at,
        created_by=str(connection.created_by) if connection.created_by else None,
    )


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    connection_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    deleted = await connection_service.delete_connection(db, connection_id, user.tenant_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Connection not found")

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="connection",
        action="connection.delete",
        actor_id=user.id,
        resource_type="connection",
        resource_id=str(connection_id),
    )
    await db.commit()


@router.patch("/{connection_id}", response_model=ConnectionResponse)
async def update_connection(
    connection_id: uuid.UUID,
    request: ConnectionUpdate,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    connection = await connection_service.get_connection(db, connection_id, user.tenant_id)
    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")

    if request.label is not None:
        connection.label = request.label
    if request.auth_type is not None:
        connection.auth_type = request.auth_type

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="connection",
        action="connection.update",
        actor_id=user.id,
        resource_type="connection",
        resource_id=str(connection_id),
        payload={"label": request.label, "auth_type": request.auth_type},
    )
    await db.commit()
    await db.refresh(connection)

    return ConnectionResponse(
        id=str(connection.id),
        tenant_id=str(connection.tenant_id),
        provider=connection.provider,
        label=connection.label,
        status=connection.status,
        auth_type=connection.auth_type,
        encryption_key_version=connection.encryption_key_version,
        metadata_json=connection.metadata_json,
        created_at=connection.created_at,
        created_by=str(connection.created_by) if connection.created_by else None,
    )


@router.post("/{connection_id}/reconnect")
async def reconnect_connection(
    connection_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Re-initiate OAuth flow for a disconnected/expired connection.

    For OAuth2 connections: returns an authorize_url to open in a popup.
    For non-OAuth connections: flips status back to active.
    """
    connection = await connection_service.get_connection(db, connection_id, user.tenant_id)
    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")

    # OAuth2 connections need a full re-authorization flow
    if connection.auth_type == "oauth2" and connection.provider == "netsuite":
        from app.core.config import settings

        if not settings.NETSUITE_OAUTH_CLIENT_ID:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="NETSUITE_OAUTH_CLIENT_ID is not configured",
            )

        import redis.asyncio as aioredis

        from app.services.netsuite_oauth_service import build_authorize_url, generate_pkce_pair

        account_id = (connection.metadata_json or {}).get("account_id", "")
        if not account_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Connection missing account_id in metadata",
            )

        restlet_url = (connection.metadata_json or {}).get("restlet_url", "")
        code_verifier, code_challenge = generate_pkce_pair()
        state = uuid.uuid4().hex

        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await r.setex(
            f"netsuite_oauth:{state}",
            600,
            f"{code_verifier}:{account_id}:{user.tenant_id}:{user.id}:{restlet_url}",
        )
        await r.aclose()

        url = build_authorize_url(account_id, state, code_challenge)

        await audit_service.log_event(
            db=db,
            tenant_id=user.tenant_id,
            category="connection",
            action="connection.reconnect",
            actor_id=user.id,
            resource_type="connection",
            resource_id=str(connection_id),
            payload={"method": "oauth2_reauthorize"},
        )
        await db.commit()

        return {"authorize_url": url, "state": state}

    # Non-OAuth connections: simple status flip
    connection.status = "active"

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="connection",
        action="connection.reconnect",
        actor_id=user.id,
        resource_type="connection",
        resource_id=str(connection_id),
    )
    await db.commit()
    await db.refresh(connection)

    return ConnectionResponse(
        id=str(connection.id),
        tenant_id=str(connection.tenant_id),
        provider=connection.provider,
        label=connection.label,
        status=connection.status,
        auth_type=connection.auth_type,
        encryption_key_version=connection.encryption_key_version,
        metadata_json=connection.metadata_json,
        created_at=connection.created_at,
        created_by=str(connection.created_by) if connection.created_by else None,
    )


@router.post("/{connection_id}/test", response_model=ConnectionTestResponse)
async def test_connection(
    connection_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await connection_service.test_connection(db, connection_id, user.tenant_id)
    return ConnectionTestResponse(**result)


# ---------------------------------------------------------------------------
# Credential / metadata update endpoints
# ---------------------------------------------------------------------------


@router.patch("/{connection_id}/client-id")
async def update_client_id(
    connection_id: uuid.UUID,
    request: ClientIdUpdate,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update the OAuth Client ID for a connection."""
    conn = await connection_service.get_connection(db, connection_id, user.tenant_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    creds = decrypt_credentials(conn.encrypted_credentials)
    creds["client_id"] = request.client_id
    conn.encrypted_credentials = encrypt_credentials(creds)

    await audit_service.log_event(
        db=db, tenant_id=user.tenant_id, category="connection",
        action="connection.update_client_id", actor_id=user.id,
        resource_type="connection", resource_id=str(connection_id),
    )
    await db.commit()
    return {"status": "ok", "client_id": request.client_id}


@router.patch("/{connection_id}/restlet-url")
async def update_restlet_url(
    connection_id: uuid.UUID,
    request: RestletUrlUpdate,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update the RESTlet URL for a connection."""
    conn = await connection_service.get_connection(db, connection_id, user.tenant_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    metadata = conn.metadata_json or {}
    metadata["restlet_url"] = request.restlet_url
    conn.metadata_json = metadata
    # Force SQLAlchemy to detect the change on JSON column
    flag_modified(conn, "metadata_json")

    await audit_service.log_event(
        db=db, tenant_id=user.tenant_id, category="connection",
        action="connection.update_restlet_url", actor_id=user.id,
        resource_type="connection", resource_id=str(connection_id),
    )
    await db.commit()
    return {"status": "ok", "restlet_url": request.restlet_url}
