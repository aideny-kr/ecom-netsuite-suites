import uuid
from typing import Annotated

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import require_permission
from app.models.user import User
from app.schemas.mcp_connector import McpConnectorCreate, McpConnectorResponse, McpConnectorTestResponse
from app.services import audit_service, mcp_connector_service
from app.services.netsuite_oauth_service import (
    build_mcp_authorize_url,
    exchange_code_with_client,
    generate_pkce_pair,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/mcp-connectors", tags=["mcp-connectors"])

# HTML template for OAuth callback popup (matches netsuite_auth.py pattern)
_MCP_CALLBACK_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>NetSuite MCP Authentication</title>
  <style>
    body {{ font-family: system-ui, sans-serif; padding: 2rem; text-align: center; }}
    .success {{ color: green; }}
    .error {{ color: red; }}
  </style>
</head>
<body>
  <h3 class="{status}">
    {heading}
  </h3>
  <p>{message}</p>
  <script>
    try {{
      if (window.opener) {{
        window.opener.postMessage(
          {{ type: "{event_type}", error: "{error_detail}" }},
          "*"
        );
        setTimeout(function() {{ window.close(); }}, 1000);
      }} else {{
        setTimeout(function() {{ window.location.href = "/"; }}, 2000);
      }}
    }} catch (e) {{
      window.location.href = "/";
    }}
  </script>
</body>
</html>"""


def _mcp_callback_uri() -> str:
    """Construct the OAuth callback URI for MCP connectors.

    Reuses the same redirect URI registered in the NetSuite Integration record
    to avoid "invalid login" errors. The MCP vs regular connection flow is
    distinguished by the state key prefix in Redis, not the callback URL.
    """
    return settings.NETSUITE_OAUTH_REDIRECT_URI


def _connector_to_response(c) -> McpConnectorResponse:
    return McpConnectorResponse(
        id=str(c.id),
        tenant_id=str(c.tenant_id),
        provider=c.provider,
        label=c.label,
        server_url=c.server_url,
        auth_type=c.auth_type,
        status=c.status,
        discovered_tools=c.discovered_tools,
        is_enabled=c.is_enabled,
        encryption_key_version=c.encryption_key_version,
        metadata_json=c.metadata_json,
        created_at=c.created_at,
        created_by=str(c.created_by) if c.created_by else None,
    )


# ---------------------------------------------------------------------------
# NetSuite OAuth 2.0 PKCE flow for MCP connectors
# ---------------------------------------------------------------------------


@router.get("/netsuite/authorize")
async def netsuite_mcp_authorize(
    account_id: str,
    client_id: str,
    label: str = "",
    user: Annotated[User, Depends(require_permission("connections.manage"))] = None,
):
    """Start the OAuth 2.0 PKCE flow for a NetSuite MCP connector."""
    if not client_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="client_id is required",
        )
    if not account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="account_id is required",
        )

    code_verifier, code_challenge = generate_pkce_pair()
    state = uuid.uuid4().hex
    redirect_uri = _mcp_callback_uri()

    # Store PKCE verifier + context in Redis with 10-min TTL
    r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    await r.setex(
        f"netsuite_mcp_oauth:{state}",
        600,
        f"{code_verifier}:{account_id}:{client_id}:{user.tenant_id}:{user.id}:{label}",
    )
    await r.aclose()

    url = build_mcp_authorize_url(account_id, client_id, redirect_uri, state, code_challenge)
    return {"authorize_url": url, "state": state}


async def netsuite_mcp_callback(
    code: str,
    state: str,
    db: AsyncSession,
    _stored: str | None = None,
):
    """OAuth 2.0 callback — exchanges code for tokens and creates MCP connector.

    Called either directly via the /netsuite/callback route or delegated from
    the shared netsuite_auth callback when the state matches an MCP flow.
    When delegated, _stored is pre-fetched from Redis.
    """
    stored = _stored
    if stored is None:
        # Direct call — fetch from Redis ourselves
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        stored = await r.get(f"netsuite_mcp_oauth:{state}")
        await r.delete(f"netsuite_mcp_oauth:{state}")
        await r.aclose()

    if not stored:
        return HTMLResponse(
            _MCP_CALLBACK_HTML.format(
                status="error",
                heading="Authentication Failed",
                message="Invalid or expired state parameter. Please try again.",
                event_type="NETSUITE_MCP_AUTH_ERROR",
                error_detail="Invalid state",
            ),
            status_code=400,
        )

    try:
        parts = stored.split(":")
        code_verifier = parts[0]
        account_id = parts[1]
        client_id = parts[2]
        tenant_id = uuid.UUID(parts[3])
        user_id = uuid.UUID(parts[4])
        label = parts[5] if len(parts) > 5 else ""
        # Check for re-authorization mode
        is_reauth = len(parts) >= 8 and parts[6] == "reauth"
        reauth_connector_id = uuid.UUID(parts[7]) if is_reauth else None
    except Exception as exc:
        logger.error("netsuite_mcp.oauth2.state_parse_failed", error=str(exc), stored_length=len(stored))
        return HTMLResponse(
            _MCP_CALLBACK_HTML.format(
                status="error",
                heading="Authentication Failed",
                message="Invalid session state. Please try again.",
                event_type="NETSUITE_MCP_AUTH_ERROR",
                error_detail=str(exc)[:200],
            ),
            status_code=400,
        )

    redirect_uri = _mcp_callback_uri()

    try:
        token_data = await exchange_code_with_client(account_id, code, code_verifier, client_id, redirect_uri)
    except Exception as exc:
        logger.error("netsuite_mcp.oauth2.exchange_failed", error=str(exc), account_id=account_id)
        return HTMLResponse(
            _MCP_CALLBACK_HTML.format(
                status="error",
                heading="Authentication Failed",
                message="Token exchange failed. Please try again.",
                event_type="NETSUITE_MCP_AUTH_ERROR",
                error_detail=str(exc)[:200],
            ),
            status_code=502,
        )

    try:
        if is_reauth and reauth_connector_id:
            # Re-authorization — update existing connector's tokens
            connector = await mcp_connector_service.get_mcp_connector(db, reauth_connector_id, tenant_id)
            if connector is None:
                raise ValueError(f"Connector {reauth_connector_id} not found")
            await mcp_connector_service.update_connector_tokens(
                db=db, connector=connector, token_data=token_data,
                account_id=account_id, client_id=client_id,
            )
            logger.info("netsuite_mcp.oauth2.reauthorized", connector_id=str(connector.id))
        else:
            # New connector
            connector = await mcp_connector_service.create_netsuite_mcp_connector(
                db=db,
                tenant_id=tenant_id,
                account_id=account_id,
                client_id=client_id,
                token_data=token_data,
                label=label or None,
                created_by=user_id,
            )
    except Exception as exc:
        logger.error("netsuite_mcp.oauth2.connector_create_failed", error=str(exc))
        return HTMLResponse(
            _MCP_CALLBACK_HTML.format(
                status="error",
                heading="Connector Creation Failed",
                message="OAuth succeeded but connector creation failed. Please try again.",
                event_type="NETSUITE_MCP_AUTH_ERROR",
                error_detail=str(exc)[:200],
            ),
            status_code=500,
        )

    # Auto-discover tools from the newly connected MCP server
    try:
        from app.services.mcp_client_service import discover_tools

        tools = await discover_tools(connector, db)
        connector.discovered_tools = tools
        await db.flush()
        logger.info(
            "netsuite_mcp.oauth2.tools_discovered",
            connector_id=str(connector.id),
            tool_count=len(tools),
        )
    except Exception as exc:
        logger.warning(
            "netsuite_mcp.oauth2.tool_discovery_failed",
            connector_id=str(connector.id),
            error=str(exc),
        )

    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="mcp_connector",
        action="mcp_connector.oauth2_authorize",
        actor_id=user_id,
        resource_type="mcp_connector",
        resource_id=str(connector.id),
        payload={"provider": "netsuite_mcp", "account_id": account_id},
    )
    await db.commit()

    return HTMLResponse(
        _MCP_CALLBACK_HTML.format(
            status="success",
            heading="Authentication Successful",
            message="NetSuite MCP connector created. You can close this window.",
            event_type="NETSUITE_MCP_AUTH_SUCCESS",
            error_detail="",
        )
    )


# ---------------------------------------------------------------------------
# Re-authorize existing connector
# ---------------------------------------------------------------------------


@router.post("/{connector_id}/reauthorize")
async def reauthorize_mcp_connector(
    connector_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Start an OAuth 2.0 re-authorization flow for an existing MCP connector."""
    connector = await mcp_connector_service.get_mcp_connector(db, connector_id, user.tenant_id)
    if connector is None:
        raise HTTPException(status_code=404, detail="MCP connector not found")
    if connector.auth_type != "oauth2":
        raise HTTPException(status_code=400, detail="Only OAuth 2.0 connectors can be re-authorized")

    from app.core.encryption import decrypt_credentials

    credentials = decrypt_credentials(connector.encrypted_credentials)
    account_id = credentials.get("account_id")
    client_id = credentials.get("client_id")
    if not account_id or not client_id:
        raise HTTPException(status_code=400, detail="Connector is missing account_id or client_id")

    code_verifier, code_challenge = generate_pkce_pair()
    state = uuid.uuid4().hex
    redirect_uri = _mcp_callback_uri()

    # Store PKCE verifier + context in Redis — include connector_id for re-auth
    r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    await r.setex(
        f"netsuite_mcp_oauth:{state}",
        600,
        f"{code_verifier}:{account_id}:{client_id}:{user.tenant_id}:{user.id}:{connector.label}:reauth:{connector_id}",
    )
    await r.aclose()

    url = build_mcp_authorize_url(account_id, client_id, redirect_uri, state, code_challenge)
    return {"authorize_url": url, "state": state}


# ---------------------------------------------------------------------------
# Standard CRUD endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[McpConnectorResponse])
async def list_mcp_connectors(
    user: Annotated[User, Depends(require_permission("connections.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    connectors = await mcp_connector_service.list_mcp_connectors(db, user.tenant_id)
    return [_connector_to_response(c) for c in connectors]


@router.post("", response_model=McpConnectorResponse, status_code=status.HTTP_201_CREATED)
async def create_mcp_connector(
    request: McpConnectorCreate,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    connector = await mcp_connector_service.create_mcp_connector(
        db=db,
        tenant_id=user.tenant_id,
        provider=request.provider,
        label=request.label,
        server_url=request.server_url,
        auth_type=request.auth_type,
        credentials=request.credentials,
        created_by=user.id,
    )

    # Auto-discover tools from the newly connected MCP server
    try:
        from app.services.mcp_client_service import discover_tools

        tools = await discover_tools(connector, db)
        connector.discovered_tools = tools
        await db.flush()
    except Exception:
        logger.warning(
            "mcp_connector.create.tool_discovery_failed",
            connector_id=str(connector.id),
            exc_info=True,
        )

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="mcp_connector",
        action="mcp_connector.create",
        actor_id=user.id,
        resource_type="mcp_connector",
        resource_id=str(connector.id),
        payload={"provider": request.provider, "label": request.label, "server_url": request.server_url},
    )
    await db.commit()
    await db.refresh(connector)

    return _connector_to_response(connector)


@router.delete("/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_connector(
    connector_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    deleted = await mcp_connector_service.delete_mcp_connector(db, connector_id, user.tenant_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="MCP connector not found")

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="mcp_connector",
        action="mcp_connector.delete",
        actor_id=user.id,
        resource_type="mcp_connector",
        resource_id=str(connector_id),
    )
    await db.commit()


@router.post("/{connector_id}/test", response_model=McpConnectorTestResponse)
async def test_mcp_connector(
    connector_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await mcp_connector_service.test_mcp_connector(db, connector_id, user.tenant_id)

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="mcp_connector",
        action="mcp_connector.test",
        actor_id=user.id,
        resource_type="mcp_connector",
        resource_id=str(connector_id),
        payload={"status": result["status"]},
    )
    await db.commit()

    return McpConnectorTestResponse(**result)
