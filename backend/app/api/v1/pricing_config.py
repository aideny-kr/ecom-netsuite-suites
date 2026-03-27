"""Pricing configuration API — GET/PUT tenant pricing config."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.pricing import PricingConfigResponse, PricingConfigUpdate
from app.services import audit_service, pricing_config_service

router = APIRouter(prefix="/pricing-config", tags=["pricing-config"])


@router.get("", response_model=PricingConfigResponse)
async def get_pricing_config(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    config = await pricing_config_service.get_config(db=db, tenant_id=user.tenant_id)
    if config is None:
        from app.services.pricing_config_defaults import get_default_config

        config = await pricing_config_service.upsert_config(
            db=db,
            tenant_id=user.tenant_id,
            config_data=get_default_config(),
            updated_by=user.id,
        )
        await db.commit()
        await db.refresh(config)
    return PricingConfigResponse(
        id=str(config.id),
        tenant_id=str(config.tenant_id),
        config=config.config,
        updated_by=str(config.updated_by) if config.updated_by else None,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


@router.put("", response_model=PricingConfigResponse)
async def update_pricing_config(
    request: PricingConfigUpdate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    config = await pricing_config_service.upsert_config(
        db=db,
        tenant_id=user.tenant_id,
        config_data=request.config.model_dump(mode="json"),
        updated_by=user.id,
    )
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="pricing",
        action="pricing.config_update",
        actor_id=user.id,
        resource_type="pricing_config",
        resource_id=str(config.id),
    )
    await db.commit()
    await db.refresh(config)
    return PricingConfigResponse(
        id=str(config.id),
        tenant_id=str(config.tenant_id),
        config=config.config,
        updated_by=str(config.updated_by) if config.updated_by else None,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )
