"""Bounded diagnostics from supported persisted bindings; never execution authority."""

from sqlalchemy import select

from app.core.dependencies import has_permission
from app.models.pipeline import Schedule
from app.models.transaction_ops import TransactionConfig
from app.services.feature_flag_service import is_enabled


def config_uses(config, kind, connection_id):
    if kind == "api":
        return connection_id in (config.source_connection_id, config.netsuite_connection_id)
    replica = (config.mapping_json or {}).get("metabase_replica") or {}
    return replica.get("connector_id") == str(connection_id)


def schedule_use(schedule, kind, connection):
    if kind == "api" and (schedule.parameters or {}).get("connection_id") == str(connection.id):
        return "exact binding"
    # These executors choose a company connection at run time. Do not invent
    # an exact binding, or scan free-form instructions/SQL for identifiers.
    required = {
        ("mcp", "bigquery"): {"bigquery_sql", "report.compose"},
        ("api", "netsuite"): {"recon.run"},
        ("api", "stripe"): {"recon.run"},
    }.get((kind, connection.provider), set())
    for plan in (schedule.plan_json, schedule.pending_plan_json):
        if isinstance(plan, dict) and any(
            step.get("type") in required for step in plan.get("steps", []) if isinstance(step, dict)
        ):
            return "provider requirement"
    return None


async def connection_usage(db, user, kind, connection):
    uses = []
    limited = False
    if await has_permission(db, user.id, "schedules.manage"):
        rows = (await db.execute(select(Schedule).where(Schedule.tenant_id == user.tenant_id))).scalars().all()
        for row in rows:
            match = schedule_use(row, kind, connection)
            if match:
                uses.append(
                    {"name": row.name, "href": f"/scheduled-jobs/{row.id}", "binding": match, "active": row.is_active}
                )
    else:
        limited = True
    if (
        await has_permission(db, user.id, "connections.manage")
        and await is_enabled(db, user.tenant_id, "celigo")
        and await is_enabled(db, user.tenant_id, "reconciliation")
    ):
        rows = (
            (await db.execute(select(TransactionConfig).where(TransactionConfig.tenant_id == user.tenant_id)))
            .scalars()
            .all()
        )
        for row in rows:
            if config_uses(row, kind, connection.id):
                uses.append(
                    {
                        "name": row.name,
                        "href": "/transaction-operations/setup",
                        "binding": "exact binding",
                        "active": row.enabled,
                    }
                )
    else:
        limited = True
    return {
        "uses": uses,
        "visibility_limited": limited,
        "coverage": (
            "Saved workflow plans and transaction source bindings. Skills and other dynamic consumers "
            "select access at run time; this is not a complete impact analysis."
        ),
    }
