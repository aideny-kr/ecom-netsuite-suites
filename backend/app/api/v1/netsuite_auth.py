"""NetSuite OAuth 2.0 PKCE endpoints."""

from __future__ import annotations

import html
import json
import uuid
from collections.abc import Sequence
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


def _js_string(value: str) -> str:
    """JSON-encode a value for embedding inside an inline <script> block.

    json.dumps alone is NOT enough here: it escapes quotes but leaves `<` and `>`
    untouched, so a value containing `</script>` closes the element early and
    everything after it is parsed as HTML. Escaping the angle brackets (and `&`)
    to \\u00xx keeps the string inert wherever it lands.
    """
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _select_connection_for_account(
    candidates: Sequence[Connection], account_id: str
) -> tuple[Connection | None, str | None]:
    """Pick which existing NetSuite row an OAuth callback should land on.

    Returns (connection, switched_from). `connection` is None when the tenant has
    no NetSuite row yet. `switched_from` is the previous account id when this
    callback repoints the tenant at a DIFFERENT NetSuite account -- the caller
    audits that, because it silently changes which account every SuiteQL query,
    pivot, report and sync reads from.

    `candidates` is ordered newest-first. Taking candidates[0] unconditionally --
    the pre-2026-08-06 behaviour -- meant re-authorizing prod could overwrite a
    sandbox row, and connecting a sandbox could overwrite prod under prod's label.
    """
    if not candidates:
        return None, None

    def account_of(conn: Connection) -> str | None:
        return (conn.metadata_json or {}).get("account_id")

    # Prefer the row already bound to this account, wherever it sorts.
    for candidate in candidates:
        if account_of(candidate) == account_id:
            return candidate, None

    # Otherwise fall back to the most recent row and keep the singleton invariant.
    connection = candidates[0]
    previous = account_of(connection)
    # Rows predating metadata_json.account_id name no account, so we cannot claim
    # what they switched from -- adopt them rather than fabricate an audit record.
    return connection, previous if previous and previous != account_id else None


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


# Shown when a callback repoints the tenant's single NetSuite connection at a
# different account. Same event_type as the success page so the opener's existing
# handlers still refresh -- but this one does not self-close, and it carries the
# account ids in the postMessage payload for a future toast.
ACCOUNT_SWITCHED_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>NetSuite Authentication</title>
  <style>
    body {{ font-family: system-ui, sans-serif; padding: 2rem; text-align: center; }}
    .warn {{ color: #92400e; background: #fef3c7; border: 1px solid #f59e0b;
             border-radius: 8px; padding: 1rem; max-width: 34rem; margin: 0 auto; }}
    code {{ background: rgba(0,0,0,0.06); padding: 0.1rem 0.3rem; border-radius: 4px; }}
  </style>
</head>
<body>
  <h3>Connected — NetSuite account changed</h3>
  <div class="warn">
    <p>This tenant was connected to <code>{previous_account_id}</code> and now uses
    <code>{new_account_id}</code>.</p>
    <p><strong>Reports, SuiteQL queries, pivots and syncs will read from
    {new_account_id} from now on.</strong> If that was not intended, reconnect
    <code>{previous_account_id}</code>.</p>
  </div>
  <p>Close this window when you have read the above.</p>
  <script>
    try {{
      if (window.opener) {{
        window.opener.postMessage(
          {{
            type: "NETSUITE_AUTH_SUCCESS",
            error: "",
            accountSwitchedFrom: {previous_account_id_js},
            accountId: {new_account_id_js}
          }},
          "*"
        );
      }}
    }} catch (e) {{}}
  </script>
</body>
</html>"""


async def _get_redis() -> aioredis.Redis:
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


@router.get("/authorize")
async def authorize(
    account_id: str,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    client_id: str = "",
    restlet_url: str = "",
):
    """Start the OAuth 2.0 PKCE flow — returns the authorize URL.

    Each connection requires its own client_id from a NetSuite Integration Record.
    Falls back to settings.NETSUITE_OAUTH_CLIENT_ID only for backwards compatibility.
    """
    resolved_client_id = client_id or settings.NETSUITE_OAUTH_CLIENT_ID
    if not resolved_client_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="client_id is required — provide the Client ID from your NetSuite Integration Record",
        )

    code_verifier, code_challenge = generate_pkce_pair()
    state = uuid.uuid4().hex

    # Store PKCE verifier + client_id in Redis with 10-min TTL
    # Use pipe delimiter for restlet_url and client_id since URLs contain colons
    r = await _get_redis()
    await r.setex(
        f"netsuite_oauth:{state}",
        600,
        f"{code_verifier}:{account_id}:{user.tenant_id}:{user.id}|{restlet_url}|{resolved_client_id}",
    )
    await r.aclose()

    url = build_authorize_url(account_id, state, code_challenge, client_id=resolved_client_id)
    return {"authorize_url": url, "state": state}


@router.get("/callback", response_class=HTMLResponse)
async def callback(
    state: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    code: str | None = None,
    error: str | None = None,
):
    """OAuth 2.0 callback — exchanges code for tokens and stores connection.

    This single callback handles both regular connections and MCP connectors,
    since NetSuite requires the redirect_uri to match the Integration record.
    The flow type is determined by which Redis key prefix exists for the state.
    """
    # Handle error responses from NetSuite (e.g. scope_mismatch)
    if error or not code:
        logger.warning("netsuite.oauth2.callback_error", error=error, state=state)
        return HTMLResponse(
            CALLBACK_HTML.format(
                status="error",
                heading="Authentication Failed",
                message=f"NetSuite returned an error: {error or 'no authorization code received'}. "
                "Check that the Integration Record scopes match the requested scopes "
                "(REST Web Services, RESTlets).",
                event_type="NETSUITE_AUTH_ERROR",
                error_detail=error or "no_code",
            ),
            status_code=400,
        )

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

    # Parse: verifier:account_id:tenant_id:user_id|restlet_url|client_id
    # First split on colon (max 4 parts) for the fixed fields, then pipe for URL-safe fields
    colon_parts = stored.split(":", maxsplit=3)
    code_verifier = colon_parts[0]
    account_id = colon_parts[1]
    tenant_id_str = colon_parts[2]
    remainder = colon_parts[3] if len(colon_parts) > 3 else ""

    # remainder = "user_id|restlet_url|client_id" or "user_id:restlet_url" (legacy)
    pipe_parts = remainder.split("|")
    user_id_str = pipe_parts[0]
    restlet_url = pipe_parts[1] if len(pipe_parts) > 1 else ""
    stored_client_id = pipe_parts[2] if len(pipe_parts) > 2 else ""

    # Legacy fallback: old format was "user_id:restlet_url" with colon
    if not restlet_url and ":" in user_id_str:
        legacy_parts = user_id_str.split(":", maxsplit=1)
        user_id_str = legacy_parts[0]
        restlet_url = legacy_parts[1] if len(legacy_parts) > 1 else ""

    tenant_id = uuid.UUID(tenant_id_str)
    user_id = uuid.UUID(user_id_str)

    # Use the client_id that was provided during authorization
    resolved_client_id = stored_client_id or settings.NETSUITE_OAUTH_CLIENT_ID

    try:
        token_data = await exchange_code(account_id, code, code_verifier, client_id=resolved_client_id)
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
        "client_id": resolved_client_id,
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_at": time.time() + int(token_data.get("expires_in", 3600)),
        "account_id": account_id,
    }

    # Upsert. The tenant's NetSuite REST connection is a singleton by design: every
    # consumer resolves it as provider=="netsuite" AND status=="active" -> first()
    # (netsuite_suiteql, pivot_tool, cross_source_tool, netsuite_connectivity,
    # suitescript_sync_tool, suitescript_sync). Adding a row per account would leave
    # those call sites choosing between accounts arbitrarily, so we update in place
    # and let _select_connection_for_account decide WHICH row and whether the tenant
    # just repointed itself at a different NetSuite account.
    result = await db.execute(
        select(Connection)
        .where(
            Connection.tenant_id == tenant_id,
            Connection.provider == "netsuite",
            Connection.status != "revoked",
        )
        .order_by(Connection.updated_at.desc())
    )
    connection, switched_from = _select_connection_for_account(result.scalars().all(), account_id)

    metadata_json = {"account_id": account_id, "auth_type": "oauth2"}
    if restlet_url:
        metadata_json["restlet_url"] = restlet_url

    if connection:
        connection.encrypted_credentials = encrypt_credentials(credentials)
        connection.encryption_key_version = get_current_key_version()
        connection.auth_type = "oauth2"
        connection.status = "active"
        connection.error_reason = None
        connection.metadata_json = metadata_json
        # Only on a genuine account switch. The label used to be written on create
        # only, so a row that changed accounts kept advertising the old one -- but
        # rewriting it on EVERY callback clobbers a name the user set through
        # PATCH /connections/{id}, and a plain token re-auth has no business
        # renaming their connection.
        if switched_from:
            connection.label = f"NetSuite {account_id}"
    else:
        connection = Connection(
            tenant_id=tenant_id,
            provider="netsuite",
            label=f"NetSuite {account_id}",
            status="active",
            auth_type="oauth2",
            encrypted_credentials=encrypt_credentials(credentials),
            encryption_key_version=get_current_key_version(),
            metadata_json=metadata_json,
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

    if switched_from:
        # The tenant just repointed its single NetSuite connection at another
        # account. Everything downstream -- SuiteQL, pivots, reports, syncs --
        # follows it, so this gets its own audit action rather than hiding inside
        # the generic authorize event.
        logger.warning(
            "netsuite.oauth2.account_switched",
            tenant_id=str(tenant_id),
            connection_id=str(connection.id),
            previous_account_id=switched_from,
            new_account_id=account_id,
        )
        await audit_service.log_event(
            db=db,
            tenant_id=tenant_id,
            category="connection",
            action="connection.account_switched",
            actor_id=user_id,
            resource_type="connection",
            resource_id=str(connection.id),
            payload={
                "provider": "netsuite",
                "previous_account_id": switched_from,
                "new_account_id": account_id,
            },
        )

    await db.commit()

    if switched_from:
        # Deliberately NOT the auto-closing template: this window stays up until
        # the operator dismisses it. The generic one self-closes after a second,
        # which is not a way to tell somebody their reporting just moved accounts.
        # account_id originates from a user-supplied query param on /authorize and
        # rides through Redis unchanged, so it is untrusted here: escape for the
        # HTML body and JSON-encode for the <script> context.
        return HTMLResponse(
            ACCOUNT_SWITCHED_HTML.format(
                previous_account_id=html.escape(switched_from),
                new_account_id=html.escape(account_id),
                previous_account_id_js=_js_string(switched_from),
                new_account_id_js=_js_string(account_id),
            )
        )

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
