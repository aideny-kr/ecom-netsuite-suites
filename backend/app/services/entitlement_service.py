import uuid

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection
from app.models.pipeline import Schedule
from app.models.tenant import Tenant

logger = structlog.get_logger()

PLAN_LIMITS = {
    "free": {
        "max_connections": 2,
        "max_schedules": 5,
        "max_exports_per_day": 10,
        "mcp_tools": False,
        "chat": True,
        "byok_ai": False,
        "workspace": False,
    },
    "pro": {
        "max_connections": 50,
        "max_schedules": 50,
        "max_exports_per_day": 1000,
        "mcp_tools": True,
        "chat": True,
        "byok_ai": True,
        "workspace": True,
    },
    "max": {
        "max_connections": -1,
        "max_schedules": 500,
        "max_exports_per_day": -1,
        "mcp_tools": True,
        "chat": True,
        "byok_ai": True,
        "workspace": True,
    },
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

    limits = PLAN_LIMITS.get(tenant.plan, PLAN_LIMITS["free"])

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
        if max_allowed == -1:
            return True
        return current_count < max_allowed

    if feature == "connections:netsuite":
        # NetSuite is always allowed for active tenants
        return True

    if feature == "schedules":
        count_result = await db.execute(
            select(func.count(Schedule.id)).where(
                Schedule.tenant_id == tenant_id,
                Schedule.is_active.is_(True),
            )
        )
        current_count = count_result.scalar() or 0
        max_allowed = limits["max_schedules"]
        if max_allowed == -1:
            return True
        return current_count < max_allowed

    if feature == "mcp_tools":
        return limits["mcp_tools"]

    if feature == "chat":
        return limits["chat"]

    if feature == "byok_ai":
        return limits["byok_ai"]

    if feature == "workspace":
        return limits["workspace"]

    return True


async def get_plan_limits(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        return PLAN_LIMITS["free"]
    return PLAN_LIMITS.get(tenant.plan, PLAN_LIMITS["free"])


async def get_usage_summary(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Return current usage counts for a tenant."""
    conn_result = await db.execute(
        select(func.count(Connection.id)).where(
            Connection.tenant_id == tenant_id,
            Connection.provider != "netsuite",
        )
    )
    connections = conn_result.scalar() or 0

    sched_result = await db.execute(
        select(func.count(Schedule.id)).where(
            Schedule.tenant_id == tenant_id,
            Schedule.is_active.is_(True),
        )
    )
    schedules = sched_result.scalar() or 0

    return {
        "connections": connections,
        "schedules": schedules,
    }
