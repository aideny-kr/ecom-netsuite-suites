"""NetSuite OAuth 2.0 PKCE endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import require_permission
from app.core.encryption import encrypt_credentials, get_current_key_version
from app.models.connection import Connection
from app.models.user import User
from app.services import audit_service
from app.services.netsuite_oauth_service import (
    build_authorize_url,
    exchange_code,
    generate_pkce_pair,
    get_valid_token,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/connections/netsuite", tags=["netsuite-oauth"])

CALLBACK_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>NetSuite Authentication</title>
  <style>
    body {{ font-family: system-ui, sans-serif; padding: 2rem; text-align: center; }}
    .success {{ color: green; }}
    .error {{ color: red; }}
    a {{ color: #0066cc; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
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


async def _get_redis() -> aioredis.Redis:
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


@router.get("/authorize")
async def authorize(
    account_id: str,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
):
    """Start the OAuth 2.0 PKCE flow — returns the authorize URL."""
    if not settings.NETSUITE_OAUTH_CLIENT_ID:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="NETSUITE_OAUTH_CLIENT_ID is not configured",
        )

    code_verifier, code_challenge = generate_pkce_pair()
    state = uuid.uuid4().hex

    # Store PKCE verifier in Redis with 10-min TTL
    r = await _get_redis()
    await r.setex(
        f"netsuite_oauth:{state}",
        600,
        f"{code_verifier}:{account_id}:{user.tenant_id}:{user.id}",
    )
    await r.aclose()

    url = build_authorize_url(account_id, state, code_challenge)
    return {"authorize_url": url, "state": state}


@router.get("/callback", response_class=HTMLResponse)
async def callback(
    code: str,
    state: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """OAuth 2.0 callback — exchanges code for tokens and stores connection.

    This single callback handles both regular connections and MCP connectors,
    since NetSuite requires the redirect_uri to match the Integration record.
    The flow type is determined by which Redis key prefix exists for the state.
    """
    r = await _get_redis()

    # Check if this is an MCP connector OAuth flow
    mcp_stored = await r.get(f"netsuite_mcp_oauth:{state}")
    if mcp_stored:
        await r.delete(f"netsuite_mcp_oauth:{state}")
        await r.aclose()
        # Delegate to MCP connector callback handler
        try:
            from app.api.v1.mcp_connectors import netsuite_mcp_callback
            return await netsuite_mcp_callback(code=code, state=state, db=db, _stored=mcp_stored)
        except Exception as exc:
            logger.error("netsuite.mcp_callback_delegation_failed", error=str(exc))
            return HTMLResponse(
                CALLBACK_HTML.format(
                    status="error",
                    heading="Authentication Failed",
                    message=f"MCP connector creation failed: {str(exc)[:200]}",
                    event_type="NETSUITE_MCP_AUTH_ERROR",
                    error_detail=str(exc)[:200],
                ),
                status_code=500,
            )

    stored = await r.get(f"netsuite_oauth:{state}")
    await r.delete(f"netsuite_oauth:{state}")
    await r.aclose()

    if not stored:
        return HTMLResponse(
            CALLBACK_HTML.format(
                status="error",
                heading="Authentication Failed",
                message="Invalid or expired state parameter. Please try again.",
                event_type="NETSUITE_AUTH_ERROR",
                error_detail="Invalid state",
            ),
            status_code=400,
        )

    code_verifier, account_id, tenant_id_str, user_id_str = stored.split(":", 3)
    tenant_id = uuid.UUID(tenant_id_str)
    user_id = uuid.UUID(user_id_str)

    try:
        token_data = await exchange_code(account_id, code, code_verifier)
    except Exception as exc:
        logger.error("netsuite.oauth2.exchange_failed", error=str(exc))
        return HTMLResponse(
            CALLBACK_HTML.format(
                status="error",
                heading="Authentication Failed",
                message="Token exchange failed. Please try again.",
                event_type="NETSUITE_AUTH_ERROR",
                error_detail=str(exc)[:200],
            ),
            status_code=502,
        )

    import time

    credentials = {
        "auth_type": "oauth2",
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_at": time.time() + int(token_data.get("expires_in", 3600)),
        "account_id": account_id,
    }

    # Upsert: update existing active netsuite connection or create new one
    result = await db.execute(
        select(Connection).where(
            Connection.tenant_id == tenant_id,
            Connection.provider == "netsuite",
            Connection.status == "active",
        )
    )
    connection = result.scalars().first()

    if connection:
        connection.encrypted_credentials = encrypt_credentials(credentials)
        connection.encryption_key_version = get_current_key_version()
    else:
        connection = Connection(
            tenant_id=tenant_id,
            provider="netsuite",
            label=f"NetSuite {account_id}",
            status="active",
            encrypted_credentials=encrypt_credentials(credentials),
            encryption_key_version=get_current_key_version(),
            created_by=user_id,
        )
        db.add(connection)

    await db.flush()

    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="connection",
        action="connection.oauth2_authorize",
        actor_id=user_id,
        resource_type="connection",
        resource_id=str(connection.id),
        payload={"provider": "netsuite", "account_id": account_id},
    )
    await db.commit()

    return HTMLResponse(
        CALLBACK_HTML.format(
            status="success",
            heading="Authentication Successful",
            message="You can close this window now.",
            event_type="NETSUITE_AUTH_SUCCESS",
            error_detail="",
        )
    )


@router.post("/{connection_id}/refresh")
async def refresh(
    connection_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Manually trigger an OAuth 2.0 token refresh."""
    result = await db.execute(
        select(Connection).where(
            Connection.id == connection_id,
            Connection.tenant_id == user.tenant_id,
            Connection.provider == "netsuite",
        )
    )
    connection = result.scalars().first()
    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")

    token = await get_valid_token(db, connection)
    if not token:
        raise HTTPException(
            status_code=502,
            detail="Token refresh failed — re-authorize via OAuth flow",
        )

    await db.commit()
    return {"status": "ok", "message": "Token refreshed successfully"}
