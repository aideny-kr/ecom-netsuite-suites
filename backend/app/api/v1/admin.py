"""Super admin router — platform-level tenant and billing management.

All endpoints require `global_role == 'superadmin'`. These bypass RLS
to allow cross-tenant reads.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_superadmin
from app.core.security import create_access_token
from app.models.tenant import Tenant
from app.models.tenant_wallet import TenantWallet
from app.models.user import User
from app.schemas.admin import (
    AdminTenantResponse,
    ImpersonateResponse,
    PlatformStatsResponse,
    WalletResponse,
    WalletUpdateRequest,
)
from app.services.audit_service import log_event

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/tenants", response_model=list[AdminTenantResponse])
async def list_tenants(
    admin: Annotated[User, Depends(get_current_superadmin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List all tenants with user count and wallet summary. Bypasses RLS."""
    # Clear RLS so we see all tenants
    await db.execute(text("RESET app.current_tenant_id"))

    result = await db.execute(
        select(Tenant).order_by(Tenant.created_at.desc())
    )
    tenants = result.scalars().all()

    # Batch-load user counts
    user_counts_result = await db.execute(
        select(User.tenant_id, func.count(User.id))
        .where(User.is_active.is_(True))
        .group_by(User.tenant_id)
    )
    user_counts = dict(user_counts_result.all())

    # Batch-load wallets
    wallets_result = await db.execute(select(TenantWallet))
    wallets = {w.tenant_id: w for w in wallets_result.scalars().all()}

    items = []
    for t in tenants:
        wallet = wallets.get(t.id)
        items.append(
            AdminTenantResponse(
                id=str(t.id),
                name=t.name,
                slug=t.slug,
                plan=t.plan,
                is_active=t.is_active,
                created_at=t.created_at,
                user_count=user_counts.get(t.id, 0),
                wallet=WalletResponse(
                    tenant_id=str(wallet.tenant_id),
                    stripe_customer_id=wallet.stripe_customer_id,
                    stripe_subscription_item_id=wallet.stripe_subscription_item_id,
                    billing_period_start=wallet.billing_period_start,
                    billing_period_end=wallet.billing_period_end,
                    base_credits_remaining=wallet.base_credits_remaining,
                    metered_credits_used=wallet.metered_credits_used,
                    last_synced_metered_credits=wallet.last_synced_metered_credits,
                ) if wallet else None,
            )
        )
    return items


@router.get("/tenants/{tenant_id}/wallet", response_model=WalletResponse | None)
async def get_tenant_wallet(
    tenant_id: uuid.UUID,
    admin: Annotated[User, Depends(get_current_superadmin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get wallet details for a specific tenant."""
    await db.execute(text("RESET app.current_tenant_id"))

    result = await db.execute(
        select(TenantWallet).where(TenantWallet.tenant_id == tenant_id)
    )
    wallet = result.scalar_one_or_none()
    if not wallet:
        return None
    return WalletResponse(
        tenant_id=str(wallet.tenant_id),
        stripe_customer_id=wallet.stripe_customer_id,
        stripe_subscription_item_id=wallet.stripe_subscription_item_id,
        billing_period_start=wallet.billing_period_start,
        billing_period_end=wallet.billing_period_end,
        base_credits_remaining=wallet.base_credits_remaining,
        metered_credits_used=wallet.metered_credits_used,
        last_synced_metered_credits=wallet.last_synced_metered_credits,
    )


@router.patch("/tenants/{tenant_id}/wallet", response_model=WalletResponse)
async def update_tenant_wallet(
    tenant_id: uuid.UUID,
    body: WalletUpdateRequest,
    admin: Annotated[User, Depends(get_current_superadmin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update wallet settings (credit top-up, Stripe IDs)."""
    await db.execute(text("RESET app.current_tenant_id"))

    result = await db.execute(
        select(TenantWallet).where(TenantWallet.tenant_id == tenant_id)
    )
    wallet = result.scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wallet not found")

    if body.base_credits_remaining is not None:
        wallet.base_credits_remaining = body.base_credits_remaining
    if body.stripe_customer_id is not None:
        wallet.stripe_customer_id = body.stripe_customer_id
    if body.stripe_subscription_item_id is not None:
        wallet.stripe_subscription_item_id = body.stripe_subscription_item_id

    await log_event(
        db=db,
        tenant_id=tenant_id,
        category="admin",
        action="admin.wallet_update",
        actor_id=admin.id,
        resource_type="tenant_wallet",
        resource_id=str(wallet.id),
        payload=body.model_dump(exclude_none=True),
    )
    await db.commit()
    await db.refresh(wallet)

    return WalletResponse(
        tenant_id=str(wallet.tenant_id),
        stripe_customer_id=wallet.stripe_customer_id,
        stripe_subscription_item_id=wallet.stripe_subscription_item_id,
        billing_period_start=wallet.billing_period_start,
        billing_period_end=wallet.billing_period_end,
        base_credits_remaining=wallet.base_credits_remaining,
        metered_credits_used=wallet.metered_credits_used,
        last_synced_metered_credits=wallet.last_synced_metered_credits,
    )


@router.post("/tenants/{tenant_id}/impersonate", response_model=ImpersonateResponse)
async def impersonate_tenant(
    tenant_id: uuid.UUID,
    admin: Annotated[User, Depends(get_current_superadmin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Generate a short-lived JWT scoped to the target tenant for impersonation."""
    await db.execute(text("RESET app.current_tenant_id"))

    tenant_result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = tenant_result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    # Find the first active owner/admin of this tenant to impersonate as
    user_result = await db.execute(
        select(User).where(User.tenant_id == tenant_id, User.is_active.is_(True)).limit(1)
    )
    target_user = user_result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active user in tenant")

    token = create_access_token(
        {"sub": str(target_user.id), "tenant_id": str(tenant_id), "impersonated_by": str(admin.id)}
    )

    await log_event(
        db=db,
        tenant_id=tenant_id,
        category="admin",
        action="admin.impersonate",
        actor_id=admin.id,
        resource_type="tenant",
        resource_id=str(tenant_id),
        payload={"target_user_id": str(target_user.id)},
    )
    await db.commit()

    return ImpersonateResponse(
        access_token=token,
        tenant_id=str(tenant_id),
        tenant_name=tenant.name,
    )


@router.get("/stats", response_model=PlatformStatsResponse)
async def get_platform_stats(
    admin: Annotated[User, Depends(get_current_superadmin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Platform-wide aggregate stats for the admin dashboard."""
    await db.execute(text("RESET app.current_tenant_id"))

    active_tenants = (await db.execute(
        select(func.count(Tenant.id)).where(Tenant.is_active.is_(True))
    )).scalar() or 0

    total_tenants = (await db.execute(
        select(func.count(Tenant.id))
    )).scalar() or 0

    total_users = (await db.execute(
        select(func.count(User.id)).where(User.is_active.is_(True))
    )).scalar() or 0

    wallet_stats = await db.execute(
        select(
            func.coalesce(func.sum(TenantWallet.base_credits_remaining), 0),
            func.coalesce(func.sum(TenantWallet.metered_credits_used), 0),
        )
    )
    base_remaining, metered_used = wallet_stats.one()

    return PlatformStatsResponse(
        active_tenants=active_tenants,
        total_tenants=total_tenants,
        total_users=total_users,
        total_base_credits_remaining=base_remaining,
        total_metered_credits_used=metered_used,
    )
