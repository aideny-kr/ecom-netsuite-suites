"""Tenant feature flag service with in-memory TTL cache."""

from __future__ import annotations

import time
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feature_flag import TenantFeatureFlag

# Default flags seeded for new tenants
DEFAULT_FLAGS: dict[str, bool] = {
    "chat": True,
    "mcp_tools": False,
    "workspace": True,
    "reconciliation": False,
    "byok_ai": False,
    "custom_branding": False,
    "custom_domain": False,
    "analytics_export": True,
}

# In-memory cache: (tenant_id, flag_key) → (enabled, timestamp)
_FLAG_CACHE: dict[tuple[uuid.UUID, str], tuple[bool, float]] = {}
_CACHE_TTL = 60  # seconds


def clear_cache() -> None:
    """Clear the flag cache (useful for tests)."""
    _FLAG_CACHE.clear()


async def is_enabled(db: AsyncSession, tenant_id: uuid.UUID, flag_key: str) -> bool:
    """Check if a feature flag is enabled for a tenant. Uses TTL cache."""
    cache_key = (tenant_id, flag_key)
    if cache_key in _FLAG_CACHE:
        value, ts = _FLAG_CACHE[cache_key]
        if time.time() - ts < _CACHE_TTL:
            return value

    result = await db.execute(
        select(TenantFeatureFlag).where(
            TenantFeatureFlag.tenant_id == tenant_id,
            TenantFeatureFlag.flag_key == flag_key,
        )
    )
    flag = result.scalar_one_or_none()
    enabled = flag.enabled if flag else False
    _FLAG_CACHE[cache_key] = (enabled, time.time())
    return enabled


async def get_all_flags(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, bool]:
    """Return all feature flags for a tenant as a dict."""
    result = await db.execute(
        select(TenantFeatureFlag).where(TenantFeatureFlag.tenant_id == tenant_id)
    )
    flags = result.scalars().all()
    return {f.flag_key: f.enabled for f in flags}


async def set_flag(
    db: AsyncSession, tenant_id: uuid.UUID, flag_key: str, enabled: bool
) -> TenantFeatureFlag:
    """Set a feature flag for a tenant (upsert)."""
    result = await db.execute(
        select(TenantFeatureFlag).where(
            TenantFeatureFlag.tenant_id == tenant_id,
            TenantFeatureFlag.flag_key == flag_key,
        )
    )
    flag = result.scalar_one_or_none()
    if flag:
        flag.enabled = enabled
    else:
        flag = TenantFeatureFlag(
            tenant_id=tenant_id, flag_key=flag_key, enabled=enabled
        )
        db.add(flag)

    # Invalidate cache
    cache_key = (tenant_id, flag_key)
    _FLAG_CACHE.pop(cache_key, None)

    return flag


async def set_flags_bulk(
    db: AsyncSession, tenant_id: uuid.UUID, flags: dict[str, bool]
) -> dict[str, bool]:
    """Set multiple feature flags at once."""
    for key, enabled in flags.items():
        await set_flag(db, tenant_id, key, enabled)
    return flags


async def seed_default_flags(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Seed default feature flags for a new tenant. Idempotent — skips existing flags."""
    for flag_key, enabled in DEFAULT_FLAGS.items():
        result = await db.execute(
            select(TenantFeatureFlag).where(
                TenantFeatureFlag.tenant_id == tenant_id,
                TenantFeatureFlag.flag_key == flag_key,
            )
        )
        if not result.scalar_one_or_none():
            db.add(
                TenantFeatureFlag(
                    tenant_id=tenant_id, flag_key=flag_key, enabled=enabled
                )
            )
