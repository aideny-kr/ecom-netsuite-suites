"""Tenant settings endpoints — branding, domain, feature flags."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_permission
from app.models.tenant import TenantConfig
from app.models.user import User
from app.schemas.settings import (
    BrandingResponse,
    BrandingUpdate,
    DomainVerifyRequest,
    DomainVerifyResponse,
    FeatureFlagsResponse,
    FeatureFlagsUpdate,
)
from app.services import audit_service
from app.services.domain_service import get_verification_record, verify_domain
from app.services.feature_flag_service import get_all_flags, set_flags_bulk

router = APIRouter(prefix="/settings", tags=["settings"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_tenant_config(db: AsyncSession, tenant_id: uuid.UUID) -> TenantConfig:
    result = await db.execute(
        select(TenantConfig).where(TenantConfig.tenant_id == tenant_id)
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Tenant config not found")
    return config


# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------


@router.get("/branding", response_model=BrandingResponse)
async def get_branding(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get tenant branding configuration."""
    config = await _get_tenant_config(db, user.tenant_id)
    return BrandingResponse(
        brand_name=config.brand_name,
        brand_color_hsl=config.brand_color_hsl,
        brand_logo_url=config.brand_logo_url,
        brand_favicon_url=config.brand_favicon_url,
        custom_domain=config.custom_domain,
        domain_verified=config.domain_verified,
    )


@router.patch("/branding", response_model=BrandingResponse)
async def update_branding(
    request: BrandingUpdate,
    user: Annotated[User, Depends(require_permission("tenant.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update tenant branding configuration."""
    config = await _get_tenant_config(db, user.tenant_id)

    update_data = request.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(config, field, value)

    # If custom_domain changed, reset verification
    if "custom_domain" in update_data:
        config.domain_verified = False

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="settings",
        action="settings.branding_update",
        actor_id=user.id,
        resource_type="tenant_config",
        resource_id=str(config.id),
    )
    await db.commit()
    await db.refresh(config)

    return BrandingResponse(
        brand_name=config.brand_name,
        brand_color_hsl=config.brand_color_hsl,
        brand_logo_url=config.brand_logo_url,
        brand_favicon_url=config.brand_favicon_url,
        custom_domain=config.custom_domain,
        domain_verified=config.domain_verified,
    )


# ---------------------------------------------------------------------------
# Domain verification
# ---------------------------------------------------------------------------


@router.post("/verify-domain", response_model=DomainVerifyResponse)
async def verify_custom_domain(
    request: DomainVerifyRequest,
    user: Annotated[User, Depends(require_permission("tenant.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Verify custom domain ownership via DNS TXT record."""
    config = await _get_tenant_config(db, user.tenant_id)

    if config.custom_domain != request.domain:
        raise HTTPException(
            status_code=400,
            detail="Domain does not match configured custom domain.",
        )

    dns_record = get_verification_record(user.tenant_id)
    verified = await verify_domain(request.domain, user.tenant_id)

    if verified:
        config.domain_verified = True
        await audit_service.log_event(
            db=db,
            tenant_id=user.tenant_id,
            category="settings",
            action="settings.domain_verified",
            actor_id=user.id,
            resource_type="tenant_config",
            resource_id=str(config.id),
        )
        await db.commit()
        await db.refresh(config)

    return DomainVerifyResponse(
        domain=request.domain,
        verified=verified,
        dns_record=dns_record,
    )


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------


@router.get("/features", response_model=FeatureFlagsResponse)
async def get_features(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get all feature flags for the tenant."""
    flags = await get_all_flags(db, user.tenant_id)
    return FeatureFlagsResponse(flags=flags)


@router.patch("/features", response_model=FeatureFlagsResponse)
async def update_features(
    request: FeatureFlagsUpdate,
    user: Annotated[User, Depends(require_permission("tenant.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update feature flags for the tenant."""
    await set_flags_bulk(db, user.tenant_id, request.flags)

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="settings",
        action="settings.features_update",
        actor_id=user.id,
        resource_type="tenant_feature_flags",
        payload=request.flags,
    )
    await db.commit()

    flags = await get_all_flags(db, user.tenant_id)
    return FeatureFlagsResponse(flags=flags)


# ---------------------------------------------------------------------------
# Public tenant resolver (for custom domain middleware)
# ---------------------------------------------------------------------------


@router.get("/resolve-domain")
async def resolve_domain(
    domain: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Resolve a custom domain to a tenant slug. Public endpoint (no auth)."""
    from app.models.tenant import Tenant

    result = await db.execute(
        select(TenantConfig, Tenant)
        .join(Tenant, Tenant.id == TenantConfig.tenant_id)
        .where(
            TenantConfig.custom_domain == domain,
            TenantConfig.domain_verified.is_(True),
        )
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Domain not found")

    config, tenant = row
    return {"tenant_slug": tenant.slug, "tenant_id": str(tenant.id)}
