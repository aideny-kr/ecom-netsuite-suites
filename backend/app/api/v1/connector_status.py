"""Connector status and health check endpoints for data pipeline connectors.

Covers: Stripe connector status/test, NetSuite deposit sync status.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db, set_tenant_context
from app.core.dependencies import require_any_permission, require_permission
from app.core.encryption import decrypt_credentials, encrypt_credentials, get_current_key_version
from app.models.canonical import NetsuitePosting, Payout, PayoutLine
from app.models.connection import Connection
from app.models.mcp_connector import McpConnector
from app.models.pipeline import CursorState
from app.models.user import User
from app.services import audit_service, mcp_connector_service
from app.services.celigo.client import (
    CELIGO_MCP_SERVER_URL,
    CeligoAuthError,
    CeligoError,
    verify_token,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/connector-status", tags=["connector-status"])


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class StripeStatusResponse(BaseModel):
    connected: bool
    connection_id: str | None = None
    status: str = "not_configured"  # online, offline, needs_reauth, not_configured
    api_key_hint: str | None = None  # last 4 chars
    last_verified_at: str | None = None
    last_sync_at: str | None = None
    payouts_count: int = 0
    payout_lines_count: int = 0
    error_message: str | None = None


class StripeTestRequest(BaseModel):
    api_key: str


class StripeTestResponse(BaseModel):
    success: bool
    account_name: str | None = None
    account_country: str | None = None
    error: str | None = None


class StripeConnectRequest(BaseModel):
    api_key: str
    label: str = "Stripe"


class DepositSyncStatusResponse(BaseModel):
    active: bool
    netsuite_connection_id: str | None = None
    netsuite_connection_label: str | None = None
    status: str = "no_connection"  # active, no_connection, sync_failed
    last_sync_at: str | None = None
    deposits_count: int = 0
    error_message: str | None = None


class CeligoStatusResponse(BaseModel):
    connected: bool
    account_name: str | None = None
    region: str | None = None
    status: str | None = None
    agent_access: bool = False


class CeligoTestRequest(BaseModel):
    token: str
    # Literal, not bare str: client.base_url() silently falls back to US on
    # any unknown value, so region="EU" (capitalized) used to route to the US
    # host, the EU token got rejected, and the operator saw a 400 "invalid
    # token" -- blaming them for what was actually a routing bug.
    region: Literal["us", "eu"] = "us"


class CeligoTestResponse(BaseModel):
    ok: bool
    account_name: str | None = None
    error: str | None = None


class CeligoConnectRequest(BaseModel):
    token: str
    region: Literal["us", "eu"] = "us"
    label: str = "Celigo"
    # Optional -- lets the chat agent read Celigo flows/scripts/errors via a
    # separate celigo_mcp MCP connector row (Task 10). Omitting it must behave
    # exactly like the REST-only connect that existed before this field.
    agent_token: str | None = None


# ---------------------------------------------------------------------------
# Stripe endpoints
# ---------------------------------------------------------------------------


@router.get("/stripe", response_model=StripeStatusResponse)
async def get_stripe_status(
    user: Annotated[User, Depends(require_any_permission("connections.manage", "recon.run"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get Stripe connector status, sync stats, and health."""
    # Find Stripe connection
    result = await db.execute(
        select(Connection).where(
            Connection.tenant_id == user.tenant_id,
            Connection.provider == "stripe",
        )
    )
    connection = result.scalar_one_or_none()

    if not connection:
        return StripeStatusResponse(connected=False)

    # Decrypt to get API key hint
    try:
        creds = decrypt_credentials(connection.encrypted_credentials)
        api_key = creds.get("api_key", "")
        api_key_hint = f"...{api_key[-4:]}" if len(api_key) >= 4 else "****"
    except Exception:
        api_key_hint = "****"

    # Determine status
    if connection.status in ("active", "healthy"):
        conn_status = "online"
    elif connection.error_reason and "expired" in (connection.error_reason or "").lower():
        conn_status = "needs_reauth"
    else:
        conn_status = "offline"

    # Get last sync time from cursor_states
    cursor_result = await db.execute(
        select(CursorState.last_synced_at).where(
            CursorState.connection_id == connection.id,
            CursorState.object_type == "stripe_payouts",
        )
    )
    last_sync_at = cursor_result.scalar_one_or_none()

    # Count payouts and payout lines
    payout_count = (
        await db.execute(select(func.count(Payout.id)).where(Payout.tenant_id == user.tenant_id))
    ).scalar_one()

    payout_line_count = (
        await db.execute(select(func.count(PayoutLine.id)).where(PayoutLine.tenant_id == user.tenant_id))
    ).scalar_one()

    return StripeStatusResponse(
        connected=True,
        connection_id=str(connection.id),
        status=conn_status,
        api_key_hint=api_key_hint,
        last_verified_at=(connection.last_health_check_at.isoformat() if connection.last_health_check_at else None),
        last_sync_at=last_sync_at.isoformat() if last_sync_at else None,
        payouts_count=payout_count,
        payout_lines_count=payout_line_count,
        error_message=connection.error_reason,
    )


@router.post("/stripe/test", response_model=StripeTestResponse)
async def test_stripe_connection(
    request: StripeTestRequest,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Validate a Stripe API key. If api_key is empty, tests the stored connection."""
    import stripe

    api_key = request.api_key
    if not api_key:
        # Use stored key from existing connection
        result = await db.execute(
            select(Connection).where(
                Connection.tenant_id == user.tenant_id,
                Connection.provider == "stripe",
            )
        )
        conn = result.scalar_one_or_none()
        if not conn:
            return StripeTestResponse(success=False, error="No Stripe connection configured")
        try:
            creds = decrypt_credentials(conn.encrypted_credentials)
            api_key = creds.get("api_key", "")
        except Exception:
            return StripeTestResponse(success=False, error="Failed to decrypt stored key")

    try:
        stripe.api_key = api_key
        account = stripe.Account.retrieve()
        # Stripe v15: Account is a StripeObject, use attribute access
        bp = getattr(account, "business_profile", None) or {}
        settings = getattr(account, "settings", None) or {}
        dashboard = getattr(settings, "dashboard", None) or {}
        bp_name = bp.get("name") if hasattr(bp, "get") else getattr(bp, "name", None)
        dash_name = (
            dashboard.get("display_name") if hasattr(dashboard, "get") else getattr(dashboard, "display_name", None)
        )
        account_name = bp_name or dash_name
        return StripeTestResponse(
            success=True,
            account_name=account_name,
            account_country=getattr(account, "country", None),
        )
    except stripe.error.AuthenticationError:
        return StripeTestResponse(success=False, error="Invalid API key")
    except stripe.error.StripeError as e:
        return StripeTestResponse(success=False, error=str(e))
    except Exception as e:
        return StripeTestResponse(success=False, error=f"Connection failed: {e}")


@router.post("/stripe/connect", status_code=status.HTTP_201_CREATED)
async def connect_stripe(
    request: StripeConnectRequest,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a Stripe connection with the provided API key.

    Validates the key first, then creates/updates the connection record.
    """
    import stripe

    # Validate key
    try:
        stripe.api_key = request.api_key
        account = stripe.Account.retrieve()
    except stripe.error.AuthenticationError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Stripe API key",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to validate Stripe key: {e}",
        )

    # Check for existing Stripe connection
    result = await db.execute(
        select(Connection).where(
            Connection.tenant_id == user.tenant_id,
            Connection.provider == "stripe",
        )
    )
    existing = result.scalar_one_or_none()

    encrypted = encrypt_credentials({"api_key": request.api_key})
    now = datetime.now(timezone.utc)

    if existing:
        # Update existing
        existing.encrypted_credentials = encrypted
        existing.status = "active"
        existing.error_reason = None
        existing.last_health_check_at = now
        existing.label = request.label
    else:
        # Create new
        connection = Connection(
            tenant_id=user.tenant_id,
            provider="stripe",
            label=request.label,
            status="active",
            auth_type="api_key",
            encrypted_credentials=encrypted,
            created_by=user.id,
            last_health_check_at=now,
            metadata_json={
                "account_name": getattr(getattr(account, "business_profile", None), "name", None),
                "account_country": getattr(account, "country", None),
                "account_id": getattr(account, "id", None),
            },
        )
        db.add(connection)

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="connection",
        action="stripe.connect",
        actor_id=user.id,
        resource_type="connection",
        resource_id=str(existing.id) if existing else "new",
    )
    await db.commit()

    # Trigger initial sync
    try:
        from app.workers.celery_app import celery_app

        # Re-fetch to get the ID if new
        conn_result = await db.execute(
            select(Connection).where(
                Connection.tenant_id == user.tenant_id,
                Connection.provider == "stripe",
            )
        )
        conn = conn_result.scalar_one()

        celery_app.send_task(
            "tasks.stripe_sync",
            kwargs={
                "tenant_id": str(user.tenant_id),
                "connection_id": str(conn.id),
            },
            queue="sync",
        )
    except Exception as e:
        logger.warning("stripe.initial_sync_trigger_failed", error=str(e))

    return {"status": "connected", "message": "Stripe connection saved and initial sync triggered"}


@router.delete("/stripe")
async def disconnect_stripe(
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Remove the Stripe connection."""
    result = await db.execute(
        select(Connection).where(
            Connection.tenant_id == user.tenant_id,
            Connection.provider == "stripe",
        )
    )
    connection = result.scalar_one_or_none()
    if not connection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No Stripe connection found")

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="connection",
        action="stripe.disconnect",
        actor_id=user.id,
        resource_type="connection",
        resource_id=str(connection.id),
    )

    await db.delete(connection)
    await db.commit()
    return {"status": "disconnected"}


# ---------------------------------------------------------------------------
# NetSuite deposit sync endpoints
# ---------------------------------------------------------------------------


@router.get("/netsuite-deposits", response_model=DepositSyncStatusResponse)
async def get_netsuite_deposit_status(
    user: Annotated[User, Depends(require_any_permission("connections.manage", "recon.run"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get NetSuite deposit sync status — uses existing OAuth REST connection."""
    from app.services.ingestion.netsuite_deposit_sync import get_netsuite_rest_connection

    connection = await get_netsuite_rest_connection(db, str(user.tenant_id))

    if not connection:
        return DepositSyncStatusResponse(active=False, status="no_connection")

    # Get last sync time from cursor_states
    cursor_result = await db.execute(
        select(CursorState.last_synced_at).where(
            CursorState.connection_id == connection.id,
            CursorState.object_type == "netsuite_deposits",
        )
    )
    last_sync_at = cursor_result.scalar_one_or_none()

    # Count deposits
    deposit_count = (
        await db.execute(
            select(func.count(NetsuitePosting.id)).where(
                NetsuitePosting.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one()

    # Determine status
    if connection.error_reason:
        sync_status = "sync_failed"
    else:
        sync_status = "active"

    return DepositSyncStatusResponse(
        active=True,
        netsuite_connection_id=str(connection.id),
        netsuite_connection_label=connection.label,
        status=sync_status,
        last_sync_at=last_sync_at.isoformat() if last_sync_at else None,
        deposits_count=deposit_count,
        error_message=connection.error_reason,
    )


@router.post("/netsuite-deposits/sync")
async def trigger_netsuite_deposit_sync(
    user: Annotated[User, Depends(require_permission("recon.run"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Trigger a manual NetSuite deposit sync for the last 90 days."""
    from datetime import date as date_type
    from datetime import timedelta

    from app.models.job import Job
    from app.services.ingestion.netsuite_deposit_sync import (
        get_netsuite_rest_connection,
        sync_netsuite_deposits,
    )

    connection = await get_netsuite_rest_connection(db, str(user.tenant_id))
    if not connection:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No active NetSuite REST connection found",
        )

    today = date_type.today()
    date_from = today - timedelta(days=90)
    now = datetime.now(timezone.utc)

    # Create job record for visibility in Job History
    job = Job(
        tenant_id=user.tenant_id,
        job_type="tasks.netsuite_deposit_sync",
        status="running",
        connection_id=connection.id,
        started_at=now,
        parameters={"date_from": date_from.isoformat(), "date_to": today.isoformat()},
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    try:
        result = await sync_netsuite_deposits(
            db=db,
            tenant_id=str(user.tenant_id),
            date_from=date_from,
            date_to=today,
        )

        if result.errors:
            job.status = "failed"
            job.error_message = result.errors[0]
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=result.errors[0],
            )

        # Mark job complete
        job.status = "completed"
        job.completed_at = datetime.now(timezone.utc)
        job.result_summary = {
            "records_synced": result.records_synced,
            "records_new": result.records_new,
            "records_updated": result.records_updated,
        }

    except HTTPException:
        raise
    except Exception as e:
        job.status = "failed"
        job.error_message = str(e)
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
        raise

    # NOTE: the freshness cursor (object_type="netsuite_deposits") is now written
    # by sync_netsuite_deposits itself for ALL callers (manual + nightly task),
    # so no cursor write is needed here. date_to == today, so the service stamps
    # an identical cursor_value/last_synced_at.

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="sync",
        action="netsuite_deposits.sync",
        actor_id=user.id,
        resource_type="connection",
        resource_id=str(connection.id),
        payload={
            "records_synced": result.records_synced,
            "records_new": result.records_new,
        },
    )
    await db.commit()

    return {
        "status": "complete",
        "records_synced": result.records_synced,
        "records_new": result.records_new,
        "records_updated": result.records_updated,
        "job_id": str(job.id),
    }


# ---------------------------------------------------------------------------
# Celigo endpoints (read-only connector -- Plan A: connect/test/disconnect only)
# ---------------------------------------------------------------------------


async def _get_celigo_connection(db: AsyncSession, tenant_id) -> Connection | None:
    result = await db.execute(
        select(Connection).where(
            Connection.tenant_id == tenant_id,
            Connection.provider == "celigo",
            Connection.status != "revoked",
        )
    )
    return result.scalar_one_or_none()


async def _celigo_agent_access(db: AsyncSession, tenant_id) -> bool:
    """Whether the tenant has a live celigo_mcp connector giving the chat agent
    read-only Celigo tools.

    Reuses the generic active-connector lookup (rather than a bespoke query) so
    this stays correct if "active" is ever redefined.
    """
    connectors = await mcp_connector_service.get_active_connectors_for_tenant(db, tenant_id)
    return any(c.provider == "celigo_mcp" for c in connectors)


async def _upsert_celigo_mcp_connector(
    db: AsyncSession,
    tenant_id,
    agent_token: str,
    created_by,
) -> McpConnector:
    """Create (or reactivate) the celigo_mcp connector that gives the chat agent
    read-only Celigo tools -- Tasks 1-3's guards enforce the read-only boundary
    once this row exists.

    Reuses ``mcp_connector_service.create_mcp_connector`` for the actual
    construction rather than a second hand-rolled ``McpConnector(...)`` site.
    Reactivating an existing (possibly revoked) row is not something that
    generic helper does, so it's handled inline here -- mirroring how
    ``connect_celigo`` itself reactivates a revoked Connection row.
    """
    existing_result = await db.execute(
        select(McpConnector).where(
            McpConnector.tenant_id == tenant_id,
            McpConnector.provider == "celigo_mcp",
        )
    )
    existing = existing_result.scalar_one_or_none()

    if existing:
        existing.encrypted_credentials = encrypt_credentials({"token": agent_token})
        existing.encryption_key_version = get_current_key_version()
        existing.auth_type = "bearer"
        existing.server_url = CELIGO_MCP_SERVER_URL
        existing.status = "active"
        existing.is_enabled = True
        existing.error_reason = None
        connector = existing
    else:
        connector = await mcp_connector_service.create_mcp_connector(
            db=db,
            tenant_id=tenant_id,
            provider="celigo_mcp",
            label="Celigo (agent access)",
            server_url=CELIGO_MCP_SERVER_URL,
            auth_type="bearer",
            credentials={"token": agent_token},
            created_by=created_by,
        )

    # Best-effort tool discovery -- must not fail the connector row itself.
    # But "must not fail the row" is not the same as "must report success":
    # agent_token is never verified on its own (unlike the REST token, which
    # goes through verify_token before any write) -- discover_tools actually
    # reaching the server and returning tools is the ONLY proof this token is
    # good for anything. `is_enabled` is what `agent_access`
    # (both the API field and _celigo_agent_access, via
    # get_active_connectors_for_tenant) key off, so it must reflect that proof
    # rather than "a row exists". A failure also resets discovered_tools --
    # a reconnect must not leave a PREVIOUS successful discovery's tool list
    # attached to a token that just failed to enumerate tools.
    try:
        from app.services.mcp_client_service import discover_tools

        tools = await discover_tools(connector, db)
        connector.discovered_tools = tools
        connector.is_enabled = True
    except Exception:
        logger.warning(
            "celigo_mcp_connector.tool_discovery_failed",
            tenant_id=str(tenant_id),
            exc_info=True,
        )
        connector.discovered_tools = None
        connector.is_enabled = False

    await db.flush()
    return connector


@router.get("/celigo", response_model=CeligoStatusResponse)
async def get_celigo_status(
    user: Annotated[User, Depends(require_permission("connections.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get Celigo connector status."""
    agent_access = await _celigo_agent_access(db, user.tenant_id)

    connection = await _get_celigo_connection(db, user.tenant_id)
    if not connection:
        return CeligoStatusResponse(connected=False, agent_access=agent_access)

    metadata = connection.metadata_json or {}
    return CeligoStatusResponse(
        connected=True,
        account_name=metadata.get("account_name"),
        region=metadata.get("region"),
        status=connection.status,
        agent_access=agent_access,
    )


@router.post("/celigo/test", response_model=CeligoTestResponse)
async def test_celigo_connection(
    request: CeligoTestRequest,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Validate a Celigo token without persisting it."""
    try:
        info = await verify_token(request.token, request.region)
    except CeligoAuthError as exc:
        return CeligoTestResponse(ok=False, error=str(exc))
    except Exception as exc:
        return CeligoTestResponse(ok=False, error=f"Connection failed: {exc}")

    return CeligoTestResponse(ok=True, account_name=info.get("account_name"))


@router.post("/celigo/connect", response_model=CeligoStatusResponse, status_code=status.HTTP_201_CREATED)
async def connect_celigo(
    request: CeligoConnectRequest,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create (or reactivate) a Celigo connection.

    Verifies the token BEFORE any database write -- an invalid token must
    never create a row. When ``agent_token`` is supplied, also creates or
    reactivates a read-only celigo_mcp MCP connector so the chat agent gets
    Celigo tools -- but that side is best-effort: a failure there must never
    fail the REST connection (agent access is a bonus, not a requirement of
    "connected").
    """
    try:
        info = await verify_token(request.token, request.region)
    except CeligoAuthError as exc:
        # The token itself was rejected -- this is genuinely the operator's mistake.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except (CeligoError, httpx.HTTPError) as exc:
        # Celigo is unreachable or returned an unexpected non-2xx (5xx/429/etc), or the
        # request itself failed at the transport level (timeout/DNS/connection refused
        # -- verify_token does not wrap these, so httpx's own exceptions reach here).
        # Deliberately 502, not 400 like the auth-error branch above: a 400 tells the
        # operator they did something wrong, which is false when the failure is on
        # Celigo's side -- and a spike of 400s reads as user error in monitoring while
        # a spike of 502s correctly reads as an upstream outage.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not reach Celigo: {exc}",
        )

    metadata = {
        "region": request.region,
        "account_name": info.get("account_name"),
        "environment_scope": "all",
    }
    encrypted = encrypt_credentials({"token": request.token})
    now = datetime.now(timezone.utc)

    # Include revoked rows so a reconnect reactivates the same row instead of
    # violating a one-connection-per-provider-per-tenant expectation.
    existing_result = await db.execute(
        select(Connection).where(
            Connection.tenant_id == user.tenant_id,
            Connection.provider == "celigo",
        )
    )
    existing = existing_result.scalar_one_or_none()

    if existing:
        existing.encrypted_credentials = encrypted
        existing.status = "active"
        existing.error_reason = None
        existing.last_health_check_at = now
        existing.label = request.label
        existing.metadata_json = metadata
        connection = existing
    else:
        connection = Connection(
            tenant_id=user.tenant_id,
            provider="celigo",
            label=request.label,
            status="active",
            auth_type="api_key",
            encrypted_credentials=encrypted,
            created_by=user.id,
            last_health_check_at=now,
            metadata_json=metadata,
        )
        db.add(connection)
        await db.flush()

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="connection",
        action="connection.create",
        actor_id=user.id,
        resource_type="connection",
        resource_id=str(connection.id),
    )
    await db.commit()
    # SET LOCAL app.current_tenant_id is transaction-scoped and the commit above
    # just cleared it -- re-establish before ANY further DB work. That covers
    # db.refresh(connection) right below, the celigo_mcp upsert + audit log in
    # the `if request.agent_token` branch, and the `_celigo_agent_access` SELECT
    # in the `else` branch -- all of it runs after this same commit.
    await set_tenant_context(db, str(user.tenant_id))
    await db.refresh(connection)

    # Agent access (celigo_mcp) is a separate, best-effort concern from here on --
    # the REST connection above is already committed and must stand regardless
    # of what happens next. Scoped to its own SAVEPOINT (db.begin_nested()) rather
    # than a bare try/except around a plain db.rollback(): a full session rollback
    # expires EVERY object the session has loaded -- including the caller's own
    # `user` and `connection` -- and touching them afterward (even just to read
    # connection.status for the response below) throws MissingGreenlet outside
    # the request's async context. A SAVEPOINT rollback only unwinds what this
    # block itself touched.
    agent_access = False
    if request.agent_token:
        try:
            async with db.begin_nested():
                mcp_connector = await _upsert_celigo_mcp_connector(
                    db=db,
                    tenant_id=user.tenant_id,
                    agent_token=request.agent_token,
                    created_by=user.id,
                )
                await audit_service.log_event(
                    db=db,
                    tenant_id=user.tenant_id,
                    category="mcp_connector",
                    action="mcp_connector.create",
                    actor_id=user.id,
                    resource_type="mcp_connector",
                    resource_id=str(mcp_connector.id),
                    payload={"provider": "celigo_mcp"},
                )
            await db.commit()
            agent_access = mcp_connector.is_enabled
        except Exception as exc:
            logger.warning(
                "celigo.agent_connector_create_failed",
                tenant_id=str(user.tenant_id),
                error=str(exc),
            )
    else:
        agent_access = await _celigo_agent_access(db, user.tenant_id)

    return CeligoStatusResponse(
        connected=True,
        account_name=metadata["account_name"],
        region=metadata["region"],
        status=connection.status,
        agent_access=agent_access,
    )


@router.delete("/celigo", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_celigo(
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Soft-delete the Celigo connection (matches connections.py's convention).

    Also revokes the celigo_mcp connector, if one exists -- leaving the agent
    with live Celigo tools after the user disconnected Celigo would be a real
    defect (Task 10).
    """
    connection = await _get_celigo_connection(db, user.tenant_id)
    if not connection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No Celigo connection found")

    connection.status = "revoked"

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="connection",
        action="connection.delete",
        actor_id=user.id,
        resource_type="connection",
        resource_id=str(connection.id),
    )

    mcp_result = await db.execute(
        select(McpConnector).where(
            McpConnector.tenant_id == user.tenant_id,
            McpConnector.provider == "celigo_mcp",
            McpConnector.status != "revoked",
        )
    )
    mcp_connector = mcp_result.scalar_one_or_none()
    if mcp_connector:
        mcp_connector.status = "revoked"
        mcp_connector.is_enabled = False
        await audit_service.log_event(
            db=db,
            tenant_id=user.tenant_id,
            category="mcp_connector",
            action="mcp_connector.delete",
            actor_id=user.id,
            resource_type="mcp_connector",
            resource_id=str(mcp_connector.id),
        )

    await db.commit()
