from app.mcp.tools import (
    data_sample,
    health,
    netsuite_connectivity,
    netsuite_suiteql,
    recon_run,
    report_export,
    schedule_ops,
    workspace_tools,
)

TOOL_REGISTRY = {
    "health": {
        "description": "Health check — returns server status and registered tool count",
        "execute": health.execute,
        "params_schema": {},
    },
    "netsuite.suiteql": {
        "description": "Execute a SuiteQL query against NetSuite",
        "execute": netsuite_suiteql.execute,
        "params_schema": {
            "query": {"type": "string", "required": True, "description": "SuiteQL query to execute"},
            "limit": {"type": "integer", "required": False, "default": 100, "description": "Max rows to return"},
        },
    },
    "netsuite.suiteql_stub": {
        "description": "Stub SuiteQL query for testing MCP connectivity",
        "execute": netsuite_suiteql.execute,
        "params_schema": {
            "query": {"type": "string", "required": True, "description": "SuiteQL query to execute"},
            "limit": {"type": "integer", "required": False, "default": 100, "description": "Max rows to return"},
        },
    },
    "netsuite.connectivity": {
        "description": "Test NetSuite connectivity and verify credentials",
        "execute": netsuite_connectivity.execute_connectivity,
        "params_schema": {},
    },
    "data.sample_table_read": {
        "description": "Read sample data from an allowlisted table",
        "execute": data_sample.execute,
        "params_schema": {
            "table_name": {"type": "string", "required": True, "description": "Table name to read from"},
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
    "workspace.list_files": {
        "description": "List files in a workspace, optionally filtered by directory path",
        "execute": workspace_tools.execute_list_files,
        "params_schema": {
            "workspace_id": {"type": "string", "required": True, "description": "Workspace UUID"},
            "directory": {"type": "string", "required": False, "description": "Directory path prefix to filter"},
            "recursive": {"type": "boolean", "required": False, "default": True},
        },
    },
    "workspace.read_file": {
        "description": "Read the content of a single file from a workspace",
        "execute": workspace_tools.execute_read_file,
        "params_schema": {
            "workspace_id": {"type": "string", "required": True},
            "file_id": {"type": "string", "required": True, "description": "File UUID"},
            "line_start": {"type": "integer", "required": False, "default": 1},
            "line_end": {"type": "integer", "required": False},
        },
    },
    "workspace.search": {
        "description": "Search for files by name or content keyword across a workspace",
        "execute": workspace_tools.execute_search,
        "params_schema": {
            "workspace_id": {"type": "string", "required": True},
            "query": {"type": "string", "required": True, "description": "Search term"},
            "search_type": {
                "type": "string",
                "required": False,
                "default": "filename",
                "description": "'filename' or 'content'",
            },
            "limit": {"type": "integer", "required": False, "default": 20},
        },
    },
    "workspace.propose_patch": {
        "description": (
            "Propose a code change as a unified diff."
            " Creates a draft changeset for human review."
            " Does NOT apply the change."
        ),
        "execute": workspace_tools.execute_propose_patch,
        "params_schema": {
            "workspace_id": {"type": "string", "required": True},
            "file_path": {"type": "string", "required": True, "description": "Target file path"},
            "unified_diff": {"type": "string", "required": True, "description": "Unified diff in standard -/+ format"},
            "title": {"type": "string", "required": True, "description": "Summary of the change"},
            "rationale": {"type": "string", "required": False},
        },
    },
    "workspace.apply_patch": {
        "description": "Apply an approved changeset. Requires workspace.apply permission and approved status.",
        "execute": workspace_tools.execute_apply_patch,
        "params_schema": {
            "changeset_id": {"type": "string", "required": True, "description": "UUID of the approved changeset"},
        },
    },
    "workspace.run_validate": {
        "description": "Trigger an SDF validate run against workspace files. Returns run_id to poll for results.",
        "execute": workspace_tools.execute_run_validate,
        "params_schema": {
            "workspace_id": {"type": "string", "required": True, "description": "Workspace UUID"},
            "changeset_id": {
                "type": "string",
                "required": True,
                "description": "Approved changeset UUID to execute against",
            },
        },
    },
    "workspace.run_unit_tests": {
        "description": "Trigger a Jest unit test run against workspace files. Returns run_id to poll for results.",
        "execute": workspace_tools.execute_run_unit_tests,
        "params_schema": {
            "workspace_id": {"type": "string", "required": True, "description": "Workspace UUID"},
            "changeset_id": {
                "type": "string",
                "required": True,
                "description": "Approved changeset UUID to execute against",
            },
        },
    },
    "workspace.run_suiteql_assertions": {
        "description": (
            "Run SuiteQL assertions against NetSuite sandbox. Each assertion is a SELECT-only query "
            "with expected result. Returns run_id to poll for results."
        ),
        "execute": workspace_tools.execute_run_suiteql_assertions,
        "params_schema": {
            "changeset_id": {
                "type": "string",
                "required": True,
                "description": "Approved changeset UUID",
            },
            "assertions": {
                "type": "array",
                "required": True,
                "description": "Array of assertion objects with name, query, expected",
            },
        },
    },
    "workspace.deploy_sandbox": {
        "description": (
            "Deploy approved changeset to NetSuite sandbox. Requires validate + unit tests passed. "
            "Returns run_id to poll for results."
        ),
        "execute": workspace_tools.execute_deploy_sandbox,
        "params_schema": {
            "changeset_id": {
                "type": "string",
                "required": True,
                "description": "Approved changeset UUID to deploy",
            },
            "override_reason": {
                "type": "string",
                "required": False,
                "description": "Admin override reason if prerequisites not fully met",
            },
            "require_assertions": {
                "type": "boolean",
                "required": False,
                "default": False,
                "description": "Whether SuiteQL assertions must pass before deploy",
            },
        },
    },
}
