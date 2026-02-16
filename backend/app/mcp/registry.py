from app.mcp.tools import netsuite_suiteql, recon_run, report_export, schedule_ops

TOOL_REGISTRY = {
    "netsuite.suiteql": {
        "description": "Execute a SuiteQL query against NetSuite",
        "execute": netsuite_suiteql.execute,
        "params_schema": {
            "query": {"type": "string", "required": True, "description": "SuiteQL query to execute"},
            "limit": {"type": "integer", "required": False, "default": 100, "description": "Max rows to return"},
        },
    },
    "recon.run": {
        "description": "Run a payout reconciliation",
        "execute": recon_run.execute,
        "params_schema": {
            "date_from": {"type": "string", "required": True, "description": "Start date (YYYY-MM-DD)"},
            "date_to": {"type": "string", "required": True, "description": "End date (YYYY-MM-DD)"},
            "payout_ids": {"type": "array", "required": False, "description": "Specific payout IDs to reconcile"},
        },
    },
    "report.export": {
        "description": "Export a report",
        "execute": report_export.execute,
        "params_schema": {
            "report_type": {"type": "string", "required": True, "description": "Type of report"},
            "format": {"type": "string", "required": False, "default": "csv", "description": "Export format"},
            "filters": {"type": "object", "required": False, "description": "Report filters"},
        },
    },
    "schedule.create": {
        "description": "Create a scheduled job",
        "execute": schedule_ops.execute_create,
        "params_schema": {
            "name": {"type": "string", "required": True},
            "schedule_type": {"type": "string", "required": True},
            "cron": {"type": "string", "required": False},
            "params": {"type": "object", "required": False},
        },
    },
    "schedule.list": {
        "description": "List all schedules",
        "execute": schedule_ops.execute_list,
        "params_schema": {},
    },
    "schedule.run": {
        "description": "Trigger a scheduled job run",
        "execute": schedule_ops.execute_run,
        "params_schema": {
            "schedule_id": {"type": "string", "required": True},
        },
    },
}
