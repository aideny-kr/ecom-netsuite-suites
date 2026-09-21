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
        ("mcp", "bigquery"): {"bigquery_sql"},
        ("mcp", "google_sheets"): {"drive.upload"},
        ("api", "netsuite"): {"recon.run"},
        ("api", "stripe"): {"recon.run"},
    }.get((kind, connection.provider), set())
    for plan in (schedule.plan_json, schedule.pending_plan_json):
        if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
            continue
        for step in plan["steps"]:
            if not isinstance(step, dict):
                continue
            if step.get("type") in required:
                return "provider requirement"
            if step.get("type") == "report.compose":
                # Derive the provider from the existing pure recipe builder,
                # not from the generic report.compose step name. No query runs.
                from app.services.report.playbooks import build_playbook_recipe

                params = step.get("params") or {}
                if not isinstance(params, dict) or "playbook_key" not in params:
                    continue  # Report-ID refresh sources need separate review.
                try:
                    _, recipe = build_playbook_recipe(params["playbook_key"], params.get("params") or {})
                except (ValueError, TypeError, AttributeError):
                    continue
                tools = {source.get("tool") for source in recipe.get("sources", {}).values()}
                if (
                    (kind, connection.provider) == ("api", "netsuite")
                    and "netsuite_financial_report" in tools
                    or (kind, connection.provider) == ("mcp", "bigquery")
                    and "bigquery_sql" in tools
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
            "Explicit workflow connection IDs, supported step provider requirements and transaction source bindings. "
            "Report-ID refreshes, invalid or unsupported plans and dynamic skill consumers need separate review; "
            "this is not a complete impact analysis."
        ),
    }
