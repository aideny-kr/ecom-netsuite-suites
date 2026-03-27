"""CRUD for tenant pricing configuration."""
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.tenant_pricing_config import TenantPricingConfig as TenantPricingConfigModel


async def get_config(db: AsyncSession, tenant_id: uuid.UUID) -> TenantPricingConfigModel | None:
    result = await db.execute(
        select(TenantPricingConfigModel).where(TenantPricingConfigModel.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()


async def upsert_config(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    config_data: dict,
    updated_by: uuid.UUID,
) -> TenantPricingConfigModel:
    existing = await get_config(db, tenant_id)
    if existing:
        existing.config = config_data
        existing.updated_by = updated_by
        return existing
    new_config = TenantPricingConfigModel(
        tenant_id=tenant_id,
        config=config_data,
        updated_by=updated_by,
    )
    db.add(new_config)
    return new_config
