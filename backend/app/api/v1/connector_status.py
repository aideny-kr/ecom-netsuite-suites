"""Connector status and health check endpoints for data pipeline connectors.

Covers: Stripe connector status/test, NetSuite deposit sync status.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_any_permission, require_permission
from app.core.encryption import decrypt_credentials, encrypt_credentials
from app.models.canonical import NetsuitePosting, Payout, PayoutLine
from app.models.connection import Connection
from app.models.pipeline import CursorState
from app.models.user import User
from app.services import audit_service

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

    from sqlalchemy.dialects.postgresql import insert

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

    # Save cursor for tracking
    stmt = (
        insert(CursorState)
        .values(
            connection_id=connection.id,
            object_type="netsuite_deposits",
            cursor_value=today.isoformat(),
            last_synced_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_update(
            constraint="uq_cursor_states_conn_obj",
            set_={
                "cursor_value": today.isoformat(),
                "last_synced_at": datetime.now(timezone.utc),
            },
        )
    )
    await db.execute(stmt)

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
