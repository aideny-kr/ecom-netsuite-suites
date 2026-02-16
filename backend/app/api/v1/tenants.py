from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_permission
from app.models.tenant import Tenant, TenantConfig
from app.models.user import User
from app.schemas.tenant import TenantConfigResponse, TenantConfigUpdate, TenantResponse, TenantUpdate

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.get("/me", response_model=TenantResponse)
async def get_tenant(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return TenantResponse(
        id=str(tenant.id),
        name=tenant.name,
        slug=tenant.slug,
        plan=tenant.plan,
        plan_expires_at=tenant.plan_expires_at,
        is_active=tenant.is_active,
    )


@router.patch("/me", response_model=TenantResponse)
async def update_tenant(
    update: TenantUpdate,
    user: Annotated[User, Depends(require_permission("tenant.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    if update.name is not None:
        tenant.name = update.name

    await db.commit()
    await db.refresh(tenant)
    return TenantResponse(
        id=str(tenant.id),
        name=tenant.name,
        slug=tenant.slug,
        plan=tenant.plan,
        plan_expires_at=tenant.plan_expires_at,
        is_active=tenant.is_active,
    )


@router.get("/me/config", response_model=TenantConfigResponse)
async def get_tenant_config(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == user.tenant_id))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Tenant config not found")
    return TenantConfigResponse(
        id=str(config.id),
        tenant_id=str(config.tenant_id),
        subsidiaries=config.subsidiaries,
        account_mappings=config.account_mappings,
        posting_mode=config.posting_mode,
        posting_batch_size=config.posting_batch_size,
        posting_attach_evidence=config.posting_attach_evidence,
        netsuite_account_id=config.netsuite_account_id,
    )


@router.patch("/me/config", response_model=TenantConfigResponse)
async def update_tenant_config(
    update: TenantConfigUpdate,
    user: Annotated[User, Depends(require_permission("tenant.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == user.tenant_id))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Tenant config not found")

    update_data = update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(config, key, value)

    await db.commit()
    await db.refresh(config)
    return TenantConfigResponse(
        id=str(config.id),
        tenant_id=str(config.tenant_id),
        subsidiaries=config.subsidiaries,
        account_mappings=config.account_mappings,
        posting_mode=config.posting_mode,
        posting_batch_size=config.posting_batch_size,
        posting_attach_evidence=config.posting_attach_evidence,
        netsuite_account_id=config.netsuite_account_id,
    )
