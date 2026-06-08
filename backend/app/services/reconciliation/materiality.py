"""Shared recon materiality-threshold loader.

Single source of truth for resolving a tenant's (abs, pct) materiality thresholds
from ``tenant_configs``. Previously duplicated verbatim on ReconJobRunner and
OrderReconJob; both now delegate here. Behavior-preserving: returns the stored
``recon_materiality_abs`` / ``recon_materiality_pct``, falling back to the
$50 / 1% defaults when the tenant has no TenantConfig row.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import TenantConfig

# Materiality defaults when a tenant has no TenantConfig row (R2a). Mirror the
# TenantConfig.recon_materiality_* server defaults: $50 OR 1% relative.
DEFAULT_MATERIALITY_ABS = Decimal("50")
DEFAULT_MATERIALITY_PCT = Decimal("0.01")


async def load_materiality(db: AsyncSession, tenant_id: str | uuid.UUID) -> tuple[Decimal, Decimal]:
    """Load a tenant's recon materiality thresholds (abs, pct).

    Falls back to the $50 / 1% defaults when no TenantConfig row exists.
    """
    cfg = (await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_id))).scalar_one_or_none()
    if cfg is None:
        return DEFAULT_MATERIALITY_ABS, DEFAULT_MATERIALITY_PCT
    return cfg.recon_materiality_abs, cfg.recon_materiality_pct
