import uuid

import structlog
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant
from app.models.connection import Connection

logger = structlog.get_logger()

PLAN_LIMITS = {
    "trial": {"max_connections": 2, "mcp_tools": False, "max_exports_per_day": 10},
    "pro": {"max_connections": 50, "mcp_tools": True, "max_exports_per_day": 1000},
    "enterprise": {"max_connections": 500, "mcp_tools": True, "max_exports_per_day": -1},
}


async def check_entitlement(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    feature: str,
) -> bool:
    """Check if a tenant is entitled to use a feature."""
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant or not tenant.is_active:
        return False

    limits = PLAN_LIMITS.get(tenant.plan, PLAN_LIMITS["trial"])

    if feature == "connections":
        # NetSuite is the core product — always allowed, doesn't count against limit
        count_result = await db.execute(
            select(func.count(Connection.id)).where(
                Connection.tenant_id == tenant_id,
                Connection.provider != "netsuite",
            )
        )
        current_count = count_result.scalar() or 0
        max_allowed = limits["max_connections"]
        return current_count < max_allowed

    if feature == "connections:netsuite":
        # NetSuite is always allowed for active tenants
        return True

    if feature == "mcp_tools":
        return limits["mcp_tools"]

    return True


async def get_plan_limits(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        return PLAN_LIMITS["trial"]
    return PLAN_LIMITS.get(tenant.plan, PLAN_LIMITS["trial"])
