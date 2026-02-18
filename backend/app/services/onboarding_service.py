import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant_profile import TenantProfile
from app.services.audit_service import log_event

logger = structlog.get_logger()


async def create_profile(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    data: dict,
    user_id: uuid.UUID,
) -> TenantProfile:
    """Create a new draft profile version (auto-increments version)."""
    # Get next version number
    result = await db.execute(
        select(func.coalesce(func.max(TenantProfile.version), 0)).where(TenantProfile.tenant_id == tenant_id)
    )
    next_version = (result.scalar() or 0) + 1

    profile = TenantProfile(
        tenant_id=tenant_id,
        version=next_version,
        status="draft",
        industry=data.get("industry"),
        business_description=data.get("business_description"),
        netsuite_account_id=data.get("netsuite_account_id"),
        chart_of_accounts=data.get("chart_of_accounts"),
        subsidiaries=data.get("subsidiaries"),
        item_types=data.get("item_types"),
        custom_segments=data.get("custom_segments"),
        fiscal_calendar=data.get("fiscal_calendar"),
        suiteql_naming=data.get("suiteql_naming"),
    )
    db.add(profile)
    await db.flush()

    await log_event(
        db=db,
        tenant_id=tenant_id,
        category="onboarding",
        action="onboarding.profile_created",
        actor_id=user_id,
        resource_type="tenant_profile",
        resource_id=str(profile.id),
        payload={"version": next_version},
    )

    logger.info("onboarding.profile_created", tenant_id=str(tenant_id), version=next_version)
    return profile


async def confirm_profile(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    profile_id: uuid.UUID,
    user_id: uuid.UUID,
) -> TenantProfile:
    """Confirm and lock a draft profile, then trigger prompt template generation."""
    result = await db.execute(
        select(TenantProfile).where(
            TenantProfile.id == profile_id,
            TenantProfile.tenant_id == tenant_id,
        )
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise ValueError("Profile not found")
    if profile.status != "draft":
        raise ValueError(f"Profile is already {profile.status}, cannot confirm")

    # Archive any previously confirmed profiles
    prev_result = await db.execute(
        select(TenantProfile).where(
            TenantProfile.tenant_id == tenant_id,
            TenantProfile.status == "confirmed",
        )
    )
    for prev in prev_result.scalars().all():
        prev.status = "archived"

    profile.status = "confirmed"
    profile.confirmed_by = user_id
    profile.confirmed_at = datetime.now(timezone.utc)
    await db.flush()

    # Generate prompt template
    from app.services.prompt_template_service import generate_and_save_template

    await generate_and_save_template(db, tenant_id, profile)

    # Update tenant onboarding status
    from app.models.tenant import TenantConfig

    config_result = await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_id))
    config = config_result.scalar_one_or_none()
    if config:
        config.onboarding_completed_at = datetime.now(timezone.utc)

    await log_event(
        db=db,
        tenant_id=tenant_id,
        category="onboarding",
        action="onboarding.profile_confirmed",
        actor_id=user_id,
        resource_type="tenant_profile",
        resource_id=str(profile.id),
        payload={"version": profile.version},
    )

    await db.refresh(profile)

    logger.info("onboarding.profile_confirmed", tenant_id=str(tenant_id), version=profile.version)
    return profile


async def get_active_profile(
    db: AsyncSession,
    tenant_id: uuid.UUID,
) -> TenantProfile | None:
    """Return the latest confirmed profile for a tenant."""
    result = await db.execute(
        select(TenantProfile)
        .where(
            TenantProfile.tenant_id == tenant_id,
            TenantProfile.status == "confirmed",
        )
        .order_by(TenantProfile.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_profiles(
    db: AsyncSession,
    tenant_id: uuid.UUID,
) -> list[TenantProfile]:
    """List all profile versions for a tenant."""
    result = await db.execute(
        select(TenantProfile).where(TenantProfile.tenant_id == tenant_id).order_by(TenantProfile.version.desc())
    )
    return list(result.scalars().all())


async def get_profile(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    profile_id: uuid.UUID,
) -> TenantProfile | None:
    """Get a specific profile by ID."""
    result = await db.execute(
        select(TenantProfile).where(
            TenantProfile.id == profile_id,
            TenantProfile.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


async def discover_netsuite_metadata(
    db: AsyncSession,
    tenant_id: uuid.UUID,
) -> dict:
    """Discover NetSuite metadata via SuiteQL (chart of accounts, subsidiaries, item types).

    Returns a dict of discovered metadata that can be used to populate a profile.
    """
    # This would call SuiteQL tools in production — for now return a placeholder structure
    logger.info("onboarding.netsuite_discovery_started", tenant_id=str(tenant_id))
    return {
        "chart_of_accounts": [],
        "subsidiaries": [],
        "item_types": [],
        "status": "discovery_not_implemented",
    }
