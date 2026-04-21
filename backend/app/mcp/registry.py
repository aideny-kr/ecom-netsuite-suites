from app.mcp.tools import (
    bigquery_tools,
    data_sample,
    health,
    netsuite_connectivity,
    netsuite_financial_report,
    netsuite_metadata_tool,
    netsuite_report,
    netsuite_suiteql,
    pivot_tool,
    pricing_tools,
    rag_search,
    recon_approve,
    recon_evidence,
    recon_exceptions,
    recon_run,
    report_export,
    save_learned_rule,
    schedule_ops,
    sheets_tools,
    suitescript_sync_tool,
    web_search,
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
            "user_question": {
                "type": "string",
                "required": False,
                "description": "The original user question — enables post-execution judge verification",
            },
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
    "netsuite.report": {
        "description": (
            "Run a native NetSuite financial report (Income Statement, Balance Sheet, Cash Flow). "
            "Uses the MCP ns_runReport endpoint for accurate, pre-built reports. "
            "Falls back to verified SuiteQL templates if MCP is unavailable."
        ),
        "execute": netsuite_report.execute,
        "params_schema": {
            "report_type": {
                "type": "string",
                "required": True,
                "description": (
                    "Report type: 'income_statement', 'balance_sheet', 'cash_flow', "
                    "or a report title string for discovery"
                ),
            },
            "period": {
                "type": "string",
                "required": True,
                "description": (
                    "Period in ISO format: '2026-02' (month), '2026-Q1' (quarter), "
                    "'2026' (year). For balance_sheet, this is the as-of period."
                ),
            },
            "subsidiary_id": {
                "type": "integer",
                "required": False,
                "description": ("Subsidiary ID to filter. Defaults to -1 (consolidated parent)."),
            },
        },
    },
    "pivot.query_result": {
        "description": (
            "Pivot a query result into a crosstab table. Works with both SuiteQL and BigQuery. "
            "Re-executes the query without row limits and pivots server-side. Use this INSTEAD "
            "of building CASE WHEN pivot SQL manually."
        ),
        "execute": pivot_tool.execute,
        "params_schema": {
            "query": {
                "type": "string",
                "required": True,
                "description": "SQL query to pivot. Row limits (FETCH FIRST / LIMIT) stripped automatically.",
            },
            "row_field": {
                "type": "string",
                "required": True,
                "description": "Column name for row grouping (e.g., 'week_start_date')",
            },
            "column_field": {
                "type": "string",
                "required": True,
                "description": "Column name whose distinct values become pivot columns (e.g., 'platform')",
            },
            "value_field": {
                "type": "string",
                "required": True,
                "description": "Column name to aggregate into cells (e.g., 'total_qty')",
            },
            "aggregation": {
                "type": "string",
                "required": False,
                "default": "sum",
                "description": "Aggregation: 'sum', 'count', 'avg', 'max', 'min'",
            },
            "include_total": {
                "type": "boolean",
                "required": False,
                "default": True,
                "description": "Add a Total column",
            },
            "dialect": {
                "type": "string",
                "required": False,
                "default": "suiteql",
                "description": "SQL dialect: 'suiteql' or 'bigquery'",
            },
        },
    },
    "netsuite.financial_report": {
        "description": (
            "(Legacy) Run a verified financial report via SuiteQL templates "
            "(Income Statement, Balance Sheet, Trial Balance, or Trend). "
            "Prefer netsuite.report for native MCP reports."
        ),
        "execute": netsuite_financial_report.execute,
        "params_schema": {
            "report_type": {
                "type": "string",
                "required": True,
                "description": (
                    "Report type: 'income_statement', 'balance_sheet', 'trial_balance', "
                    "'income_statement_trend', or 'balance_sheet_trend'"
                ),
                "enum": [
                    "income_statement",
                    "balance_sheet",
                    "trial_balance",
                    "income_statement_trend",
                    "balance_sheet_trend",
                ],
            },
            "period": {
                "type": "string",
                "required": True,
                "description": (
                    "Period name like 'Feb 2026' or comma-separated 'Jan 2026, Feb 2026, Mar 2026' "
                    "for multi-month/trend reports. For balance_sheet, this is the as-of period."
                ),
            },
            "subsidiary_id": {
                "type": "integer",
                "required": False,
                "description": "Optional subsidiary ID to filter to a single subsidiary.",
            },
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
    "recon.get_exceptions": {
        "description": "Fetch unmatched and low-confidence reconciliation results (exceptions) for investigation",
        "execute": recon_exceptions.execute,
        "params_schema": {
            "run_id": {"type": "string", "required": True, "description": "Reconciliation run ID"},
        },
    },
    "recon.get_evidence": {
        "description": "Get evidence pack download link for a reconciliation run",
        "execute": recon_evidence.execute,
        "params_schema": {
            "run_id": {"type": "string", "required": True, "description": "Reconciliation run ID"},
        },
    },
    "recon.approve_match": {
        "description": (
            "Approve a suggested reconciliation match. REQUIRES user confirmation before execution. "
            "Show match details (Stripe amount, NetSuite amount, variance) and ask for approval."
        ),
        "execute": recon_approve.execute,
        "params_schema": {
            "result_id": {"type": "string", "required": True, "description": "ReconciliationResult ID to approve"},
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
            "sandbox_id": {
                "type": "string",
                "required": True,
                "description": "Target sandbox account identifier (for example: 6738075-sb1)",
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
    "netsuite.refresh_metadata": {
        "description": (
            "Trigger a fresh discovery of NetSuite custom fields, record types, "
            "subsidiaries, departments, classes, and locations. Updates the AI's "
            "knowledge of this account's customisations. Use when the user says "
            "'refresh metadata', 'discover custom fields', or after new customisations "
            "have been added to NetSuite."
        ),
        "execute": netsuite_metadata_tool.execute,
        "params_schema": {},
    },
    "netsuite.get_metadata": {
        "description": (
            "Return a summary of previously discovered NetSuite metadata "
            "including counts of custom fields, record types, and org hierarchy. "
            "Use to answer 'what custom fields do I have?' or 'show metadata summary'."
        ),
        "execute": netsuite_metadata_tool.execute_get_metadata,
        "params_schema": {},
    },
    "suitescript.sync": {
        "description": (
            "Sync SuiteScript files from the connected NetSuite account into the workspace. "
            "Discovers JavaScript files and custom scripts via SuiteQL, fetches their content, "
            "and loads them into the 'NetSuite Scripts' workspace for browsing and editing. "
            "Use when the user says 'sync scripts', 'load suitescripts', or 'import scripts from NetSuite'."
        ),
        "execute": suitescript_sync_tool.execute,
        "params_schema": {},
    },
    "web.search": {
        "description": (
            "Search the web for current information. Use when internal documentation "
            "(rag_search) doesn't have what you need. Good for: unfamiliar error messages, "
            "recent NetSuite updates, external API documentation, SuiteQL syntax questions "
            "not covered by stored docs, or any topic requiring up-to-date information."
        ),
        "execute": web_search.execute,
        "params_schema": {
            "query": {
                "type": "string",
                "required": True,
                "description": "Search query (be specific for better results)",
            },
            "max_results": {
                "type": "integer",
                "required": False,
                "default": 5,
                "description": "Max results to return (1-10)",
            },
        },
    },
    "tenant.save_learned_rule": {
        "description": (
            "Save a persistent business rule or preference for this tenant. "
            "Call this when the user gives you a standing instruction, correction, "
            "or preference about how queries or outputs should work (e.g., "
            "'always show Value not ID', 'stuck in Ordoro means status=5'). "
            "Only tenant admins can persist rules; non-admins get session-only memory."
        ),
        "execute": save_learned_rule.execute,
        "params_schema": {
            "rule_description": {
                "type": "string",
                "required": True,
                "description": "The business rule or preference to remember across sessions",
            },
            "rule_category": {
                "type": "string",
                "required": False,
                "default": "general",
                "description": "Category: output_preference, status_mapping, query_logic, term_definition, or general",
            },
        },
    },
    "rag.search": {
        "description": (
            "Search documentation, SuiteQL reference rules, and domain knowledge. "
            "Use PROACTIVELY when: a query fails with syntax errors, you need correct "
            "column names, date functions, status codes, or record type IDs. "
            "This searches the golden dataset of expert-verified rules."
        ),
        "execute": rag_search.execute,
        "params_schema": {
            "query": {"type": "string", "required": True, "description": "Natural language search query"},
            "top_k": {"type": "integer", "required": False, "default": 10, "description": "Max results to return"},
            "source_filter": {
                "type": "string",
                "required": False,
                "description": "Filter by source path prefix (e.g. 'netsuite_metadata/')",
            },
        },
    },
    "bigquery.sql": {
        "description": (
            "Execute a read-only BigQuery SQL query. Returns columns, rows, and metadata. "
            "Cost-guardrailed via max_bytes_billed. Use for analytics and BI queries."
        ),
        "execute": bigquery_tools.bigquery_sql_execute,
        "params_schema": {
            "query": {"type": "string", "required": True, "description": "BigQuery SQL query (SELECT/WITH only)"},
            "max_rows": {
                "type": "integer",
                "required": False,
                "default": 1000,
                "description": "Maximum rows to return (default 1000)",
            },
        },
    },
    "bigquery.schema": {
        "description": (
            "Discover BigQuery datasets, tables, and columns. Use to explore available data before writing queries."
        ),
        "execute": bigquery_tools.bigquery_schema_execute,
        "params_schema": {
            "dataset": {
                "type": "string",
                "required": False,
                "description": "Specific dataset to inspect (omit for all datasets)",
            },
        },
    },
    "bigquery.cost_estimate": {
        "description": (
            "Estimate the cost of a BigQuery query via dry run. "
            "Returns estimated bytes and USD cost ($5/TB). "
            "Use before running expensive queries."
        ),
        "execute": bigquery_tools.bigquery_cost_estimate_execute,
        "params_schema": {
            "query": {"type": "string", "required": True, "description": "BigQuery SQL query to estimate"},
        },
    },
    "sheets.create": {
        "description": (
            "Create a new Google Spreadsheet. Returns the spreadsheet ID and URL. "
            "The sheet is automatically shared with the requesting user."
        ),
        "execute": sheets_tools.sheets_create_execute,
        "params_schema": {
            "title": {
                "type": "string",
                "required": True,
                "description": "Title for the new spreadsheet",
            },
        },
    },
    "sheets.write_range": {
        "description": (
            "Write data to a Google Spreadsheet. Data should be a 2D array where row 0 is "
            "headers. Returns the updated range and row count."
        ),
        "execute": sheets_tools.sheets_write_range_execute,
        "params_schema": {
            "spreadsheet_id": {
                "type": "string",
                "required": True,
                "description": "ID of the spreadsheet to write to",
            },
            "data": {
                "type": "array",
                "required": True,
                "description": "2D array of values. Row 0 should be column headers.",
            },
            "range": {
                "type": "string",
                "required": False,
                "description": "Cell range to write to (default: Sheet1!A1)",
            },
        },
    },
    "sheets.read_range": {
        "description": (
            "Read cell values from an existing Google Spreadsheet. "
            "Use this to reference or ingest data the user already has in a "
            "Google Sheet. The service account must have access — sheets in "
            "the connector's configured Shared Drive work automatically; "
            "other sheets need to be shared with the service account email "
            "(visible in Settings → Data Source Connectors → Google Sheets). "
            "Returns a 2D `values` array (row 0 is typically the header row) "
            "plus the actual range that was returned."
        ),
        "execute": sheets_tools.sheets_read_range_execute,
        "params_schema": {
            "spreadsheet_id": {
                "type": "string",
                "required": True,
                "description": (
                    "The Google Sheet ID. Extract from the URL: "
                    "https://docs.google.com/spreadsheets/d/<ID>/edit"
                ),
            },
            "range": {
                "type": "string",
                "required": False,
                "description": (
                    "A1 notation range (e.g. 'Sheet1!A1:D100' or 'Sheet1' for "
                    "the whole first tab). Defaults to 'Sheet1'."
                ),
            },
        },
    },
    "pricing.convert": {
        "description": (
            "Convert product prices from USD to multiple currencies using tenant FX rates, VAT rules, and rounding. "
            "Input: file_id of uploaded Excel. Output: Excel + NetSuite CSV file_ids for download."
        ),
        "execute": pricing_tools.pricing_convert_execute,
        "params_schema": {
            "file_id": {"type": "string", "required": True, "description": "ID of the uploaded Excel file"},
        },
    },
    "pricing.config_read": {
        "description": (
            "Read the current tenant pricing configuration — FX rates, VAT/GST percentages, and rounding rules."
        ),
        "execute": pricing_tools.pricing_config_read_execute,
        "params_schema": {},
    },
    "pricing.config_update": {
        "description": (
            "Update the tenant pricing configuration — change FX rates, VAT/GST percentages, or rounding rules. "
            "Pass updates as a dict with 'eur_fx_rate' and/or 'currencies' containing per-currency changes."
        ),
        "execute": pricing_tools.pricing_config_update_execute,
        "params_schema": {
            "updates": {
                "type": "object",
                "required": True,
                "description": (
                    "Fields to update. Example: {eur_fx_rate: 1.08, currencies: {GBP: {fx_rate: 0.79, vat_rate: 20}}}"
                ),
            },
        },
    },
    "pricing.export": {
        "description": (
            "Generate downloadable Excel + NetSuite CSV from inline pricing data (no file upload needed). "
            "Pass items as a list of {sku, usd_price, item_name?}. Uses tenant FX rates, VAT rules, and rounding. "
            "Returns file_ids for download."
        ),
        "execute": pricing_tools.pricing_export_execute,
        "params_schema": {
            "items": {
                "type": "array",
                "required": True,
                "description": "List of items: [{sku: string, usd_price: number, item_name?: string}]",
            },
        },
    },
}
