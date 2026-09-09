import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pipeline import Schedule

logger = structlog.get_logger()


def _as_uuid(value) -> uuid.UUID | None:
    if value is None:
        return None
    return uuid.UUID(value) if isinstance(value, str) else value


async def execute_create(params: dict, **kwargs) -> dict:
    """Create a schedule in the database via MCP context.

    Two shapes (spec §B5 — mirrors `POST /schedules`):
    - `instruction` given: the compile path. Compiles via
      `app.services.jobs.compiler.compile_instruction`; a `Clarification`
      creates NOTHING and is surfaced as `{"error": True, "clarification":
      True, "message": <question>}`, exactly like the API's `409` — the chat
      agent's caller decides how to relay that back to the operator.
    - `instruction` absent: the legacy direct-create path, unchanged from
      pre-Slice-2 behaviour.
    """
    context = kwargs.get("context", {})
    db: AsyncSession | None = context.get("db")
    tenant_id_raw = context.get("tenant_id")

    if db is None or tenant_id_raw is None:
        return {
            "error": True,
            "message": "No database context available — cannot create schedule",
        }

    tenant_id = _as_uuid(tenant_id_raw)
    actor_id = _as_uuid(context.get("actor_id"))

    instruction = params.get("instruction")
    if instruction:
        from app.services import schedule_service
        from app.services.jobs.compiler import Clarification, compile_instruction

        compiled = await compile_instruction(
            db,
            tenant_id=tenant_id,
            instruction=instruction,
            actor_id=actor_id,
            plan_version=0,
        )
        if isinstance(compiled, Clarification):
            return {"error": True, "clarification": True, "message": compiled.question}

        schedule = Schedule(
            tenant_id=tenant_id,
            name=params.get("name") or schedule_service.default_job_name(instruction),
            schedule_type="job",
            cron_expression=params.get("cron_expression") or params.get("cron"),
            timezone=params.get("timezone") or "UTC",
            is_active=True,
            instruction=instruction,
            plan_json=compiled.plan_json,
            plan_version=0,
            plan_status="pending_approval",
            delivery_json=params.get("delivery"),
            owner_id=actor_id,
            created_via="chat",
        )
        db.add(schedule)
        await db.flush()

        logger.info("mcp.schedule.created", schedule_id=str(schedule.id), tenant_id=str(tenant_id), job=True)
        return {
            "schedule_id": str(schedule.id),
            "name": schedule.name,
            "schedule_type": schedule.schedule_type,
            "plan_status": schedule.plan_status,
            "summary_line": compiled.summary_line,
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

    schedule = Schedule(
        tenant_id=tenant_id,
        name=name,
        schedule_type=schedule_type,
        cron_expression=cron_expression,
        is_active=True,
        parameters=parameters,
        owner_id=actor_id,
        created_via="chat",
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

    tenant_id = _as_uuid(tenant_id_raw)

    result = await db.execute(
        select(Schedule).where(Schedule.tenant_id == tenant_id).order_by(Schedule.created_at.desc())
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
                "plan_status": s.plan_status,
            }
            for s in schedules
        ]
    }


async def execute_run(params: dict, **kwargs) -> dict:
    """Run a schedule now (spec §B5: MCP `execute_run` "implemented — no
    longer a stub"). Delegates to the SAME `run_schedule_now` the API's
    `POST /schedules/{id}/run` calls — one `jobs` row, replaying whichever
    plan `use_pending` selects; no LLM call happens here (the agent runs only
    at compile time)."""
    context = kwargs.get("context", {})
    db: AsyncSession | None = context.get("db")
    tenant_id_raw = context.get("tenant_id")

    if db is None or tenant_id_raw is None:
        return {
            "error": True,
            "message": "No database context available — cannot run schedule",
        }

    schedule_id_raw = params.get("schedule_id")
    if not schedule_id_raw:
        return {"error": True, "message": "Missing required param: 'schedule_id'"}

    tenant_id = _as_uuid(tenant_id_raw)
    try:
        schedule_id = _as_uuid(schedule_id_raw)
    except (ValueError, TypeError):
        return {"error": True, "message": f"Invalid schedule_id: {schedule_id_raw!r}"}

    actor_id = _as_uuid(context.get("actor_id"))
    use_pending = bool(params.get("use_pending", False))

    from sqlalchemy.exc import NoResultFound

    from app.workers.tasks.scheduled_jobs import run_schedule_now

    try:
        outcome = await run_schedule_now(
            db,
            schedule_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            actor_type="user",
            use_pending=use_pending,
        )
    except NoResultFound:
        return {"error": True, "message": f"Schedule not found: {schedule_id}"}

    logger.info(
        "mcp.schedule.run",
        schedule_id=str(schedule_id),
        tenant_id=str(tenant_id),
        reason=outcome.reason,
        jobs_id=str(outcome.jobs_row_id) if outcome.jobs_row_id else None,
    )
    return {
        "run_id": str(outcome.jobs_row_id) if outcome.jobs_row_id else None,
        "jobs_id": str(outcome.jobs_row_id) if outcome.jobs_row_id else None,
        "schedule_id": str(schedule_id),
        "reason": outcome.reason,
    }
