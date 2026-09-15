"""Authenticated Metabase OAuth initiation/completion and an inert code relay."""

import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.oauth_callback_page import js_string
from app.core.database import get_db
from app.core.dependencies import require_permission
from app.models.user import User
from app.services import audit_service, mcp_connector_service
from app.services import metabase_oauth_service as oauth

router = APIRouter(prefix="/mcp-connectors", tags=["mcp-connectors"])
Manager = Annotated[User, Depends(require_permission("connections.manage"))]
Database = Annotated[AsyncSession, Depends(get_db)]


class StartRequest(BaseModel):
    app_origin: str = Field(max_length=1024)


class CompleteRequest(BaseModel):
    state: str = Field(min_length=43, max_length=43)
    code: str | None = Field(default=None, max_length=8192)
    error: str | None = Field(default=None, max_length=1024)


async def get_connector(db, connector_id, user):
    connector = await mcp_connector_service.get_mcp_connector(db, connector_id, user.tenant_id)
    if not connector or connector.status in ("revoked", "superseded") or not oauth.is_metabase(connector):
        raise HTTPException(404, "Metabase connection not found")
    return connector


@router.post("/{connector_id}/metabase/authorize")
async def authorize(connector_id: uuid.UUID, body: StartRequest, user: Manager, db: Database):
    connector = await get_connector(db, connector_id, user)
    try:
        result = await oauth.start(connector, user, body.app_origin)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    await audit_service.log_event(
        db,
        user.tenant_id,
        "connection",
        "mcp_connector.oauth_start",
        actor_id=user.id,
        resource_type="mcp_connector",
        resource_id=str(connector.id),
    )
    await db.commit()
    return result


@router.post("/{connector_id}/metabase/complete")
async def complete(connector_id: uuid.UUID, body: CompleteRequest, user: Manager, db: Database):
    connector = await get_connector(db, connector_id, user)
    try:
        result = await oauth.complete(db, connector, user, body.state, body.code, body.error)
    except oauth.OAuthError as exc:
        raise HTTPException(400, str(exc)) from None
    await audit_service.log_event(
        db,
        user.tenant_id,
        "connection",
        "mcp_connector.oauth_complete",
        actor_id=user.id,
        resource_type="mcp_connector",
        resource_id=str(connector.id),
        status="success" if result["status"] == "ok" else "error",
        payload={"verification_status": result["status"]},
    )
    await db.commit()
    return result


@router.get("/metabase/oauth/callback", response_class=HTMLResponse)
async def callback(state: str = Query(default="", max_length=128)):
    # Read code/error in the browser, never interpolate or log provider content.
    # Only the encrypted, unexpired state determines the allowed opener origin.
    try:
        pending = await oauth.load_state(state)
    except oauth.OAuthError:
        return HTMLResponse(
            "<p>Sign-in expired. Close this window and connect with Metabase again.</p>",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    nonce = secrets.token_urlsafe(24)
    return HTMLResponse(
        f'''<!doctype html><html><head><title>Metabase sign-in</title></head>
<body><p>Returning to Connections. Keep the Connections tab open to finish sign-in.</p>
<script nonce="{nonce}">
const params = new URLSearchParams(window.location.search);
window.history.replaceState(null, "", window.location.pathname);
if (window.opener) window.opener.postMessage({{
 type: "METABASE_OAUTH_RESULT", state: {js_string(state)},
 code: params.get("code"), error: params.get("error")
}}, {js_string(pending["app_origin"])});
</script></body></html>''',
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; frame-ancestors 'none'; base-uri 'none'"
            ),
        },
    )
