import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pipeline import Schedule

logger = structlog.get_logger()


async def execute_create(params: dict, **kwargs) -> dict:
    """Create a schedule in the database via MCP context."""
    context = kwargs.get("context", {})
    db: AsyncSession | None = context.get("db")
    tenant_id_raw = context.get("tenant_id")

    if db is None or tenant_id_raw is None:
        return {
            "error": True,
            "message": "No database context available — cannot create schedule",
        }

    name = params.get("name")
    schedule_type = params.get("schedule_type")
    if not name or not schedule_type:
        return {
            "error": True,
            "message": "Missing required params: 'name' and 'schedule_type'",
        }

    cron_expression = params.get("cron_expression") or params.get("cron")
    parameters = params.get("parameters")

    if isinstance(tenant_id_raw, str):
        tenant_id = uuid.UUID(tenant_id_raw)
    else:
        tenant_id = tenant_id_raw

    schedule = Schedule(
        tenant_id=tenant_id,
        name=name,
        schedule_type=schedule_type,
        cron_expression=cron_expression,
        is_active=True,
        parameters=parameters,
    )
    db.add(schedule)
    await db.flush()

    logger.info("mcp.schedule.created", schedule_id=str(schedule.id), tenant_id=str(tenant_id))
    return {
        "schedule_id": str(schedule.id),
        "name": schedule.name,
        "schedule_type": schedule.schedule_type,
        "cron_expression": schedule.cron_expression,
        "is_active": schedule.is_active,
    }


async def execute_list(params: dict, **kwargs) -> dict:
    """List schedules for the tenant from the database via MCP context."""
    context = kwargs.get("context", {})
    db: AsyncSession | None = context.get("db")
    tenant_id_raw = context.get("tenant_id")

    if db is None or tenant_id_raw is None:
        return {
            "error": True,
            "message": "No database context available — cannot list schedules",
            "schedules": [],
        }

    if isinstance(tenant_id_raw, str):
        tenant_id = uuid.UUID(tenant_id_raw)
    else:
        tenant_id = tenant_id_raw

    result = await db.execute(
        select(Schedule)
        .where(Schedule.tenant_id == tenant_id)
        .order_by(Schedule.created_at.desc())
    )
    schedules = result.scalars().all()

    return {
        "schedules": [
            {
                "schedule_id": str(s.id),
                "name": s.name,
                "schedule_type": s.schedule_type,
                "cron_expression": s.cron_expression,
                "is_active": s.is_active,
            }
            for s in schedules
        ]
    }


async def execute_run(params: dict, **kwargs) -> dict:
    """Stub: Run a schedule."""
    return {
        "run_id": str(uuid.uuid4()),
        "schedule_id": params.get("schedule_id"),
        "message": "Stub: Schedule run not yet implemented",
    }
