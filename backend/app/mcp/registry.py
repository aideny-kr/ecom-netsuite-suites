from app.mcp.tools import (
    bigquery_tools,
    cross_source_tool,
    data_sample,
    docs_tools,
    drive_tools,
    health,
    metric_tools,
    netsuite_connectivity,
    netsuite_financial_report,
    netsuite_metadata_tool,
    netsuite_suiteql,
    pivot_tool,
    pricing_tools,
    rag_search,
    recon_approve,
    recon_approve_group,
    recon_evidence,
    recon_exceptions,
    recon_resolution_summary,
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
    "cross_source.query": {
        "description": (
            "Join data across TWO sources (NetSuite SuiteQL and BigQuery) into one "
            "unified table. Pass both queries + the join key; the backend re-runs both, "
            "joins them deterministically (DuckDB), and returns one table. Use this "
            "INSTEAD of correlating two separate result tables yourself."
        ),
        "execute": cross_source_tool.execute,
        "params_schema": {
            "left_query": {"type": "string", "required": True, "description": "SQL for source A"},
            "left_dialect": {
                "type": "string",
                "required": True,
                "description": "'suiteql' or 'bigquery' for source A",
            },
            "right_query": {"type": "string", "required": True, "description": "SQL for source B"},
            "right_dialect": {
                "type": "string",
                "required": True,
                "description": "'suiteql' or 'bigquery' for source B",
            },
            "join_keys": {
                "type": "array",
                "required": True,
                "description": (
                    'Equality keys, e.g. [{"left": "sku", "right": "item"}]. '
                    "Numeric-looking keys are matched by value (123 == 123.0); avoid this tool "
                    "for zero-padded string keys (SKUs/zips with significant leading zeros)."
                ),
            },
            "join_type": {
                "type": "string",
                "required": False,
                "default": "inner",
                "description": "'inner' or 'left'",
            },
            "select": {
                "type": "array",
                "required": False,
                "description": "Optional output columns to keep (default: all)",
            },
            "pivot": {
                "type": "object",
                "required": False,
                "description": (
                    "Optional crosstab on the joined result: "
                    '{"row_field", "column_field", "value_field", "aggregation"}'
                ),
            },
        },
    },
    "netsuite.financial_report": {
        "description": (
            "Run a verified financial report via SuiteQL templates "
            "(Income Statement, Balance Sheet, Trial Balance, or Trend), using "
            "BUILTIN.CONSOLIDATE for correct multi-currency consolidation at "
            "posting-time FX. The local default for financial statements; for a "
            "native pre-built report use the external MCP ns_runReport directly."
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
        "description": (
            "Fetch open reconciliation rows for a run from ONE authoritative four-bucket population. "
            'Default bucket="needs_review" (unmatched + material-variance rows — material-variance '
            'suggested rows live HERE). bucket="rules" lists the rules bucket: fuzzy matches, mostly '
            "status=suggested awaiting approval but also pending. The close gate's Approve-Suggested-"
            "Matches count is STATUS-keyed (status=suggested across ALL buckets), so NEITHER bucket "
            "listing equals that count — to investigate what blocks the close gate, list BOTH the "
            'default needs_review bucket AND bucket="rules". Already-dispositioned (approved/locked) '
            "rows are always excluded. "
            "Returns at most 50 rows, largest absolute variance first; exception_count is the TRUE total "
            "matching the filters and truncated tells you whether rows were cut off — never present a "
            "truncated list as exhaustive. Transcribe every returned number VERBATIM into a table — never "
            "recompute, round, sum, or paraphrase amounts in prose — and quote exception_count exactly. "
            "Each row carries the authoritative status + bucket; advisory_match_score is advisory-only, "
            "never a verdict — disposition derives from status/bucket."
        ),
        "execute": recon_exceptions.execute,
        "params_schema": {
            "run_id": {"type": "string", "required": True, "description": "Reconciliation run ID"},
            "bucket": {
                "type": "string",
                "required": False,
                "description": (
                    "Bucket to list (default: needs_review). One of: matches, rules, "
                    "auto_classifications, needs_review. 'rules' = the fuzzy-match "
                    "bucket (mostly suggested, also pending)."
                ),
            },
            "min_variance": {
                "type": "string",
                "required": False,
                "description": (
                    "Optional minimum absolute variance amount to include (finite, non-negative number, e.g. '50.00')"
                ),
            },
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
    "recon.get_resolution_summary": {
        "description": (
            "Fetch the summary-first resolution report for a reconciliation run: match_rate, "
            "explained_rate, proposals_count, and resolution groups (largest total amount first, "
            "capped at 20 — group_count is the TRUE total distinct-group count and truncated tells "
            "you whether the returned list was cut off; never present a truncated list as exhaustive). "
            "Each group carries root_cause, action, booking_vehicle, currency, count, proposed_count, "
            "approved_count, total_amount, and above_materiality_count. Transcribe every returned "
            "number VERBATIM into a table — never recompute, round, sum, or paraphrase amounts in "
            "prose — and quote group_count/proposals_count exactly."
        ),
        "execute": recon_resolution_summary.execute,
        "params_schema": {
            "run_id": {"type": "string", "required": True, "description": "Reconciliation run ID"},
        },
    },
    "recon.approve_group": {
        "description": (
            "Bulk-approve a resolution group (root_cause:action:booking_vehicle). REQUIRES user "
            "confirmation before execution — a confirmation card is shown; do NOT claim the group is "
            "approved until the user explicitly confirms. needs_human groups cannot be group-approved. "
            "Above-materiality proposals only approve when explicitly included via "
            "included_above_materiality_ids."
        ),
        "execute": recon_approve_group.execute,
        "params_schema": {
            "run_id": {"type": "string", "required": True, "description": "Reconciliation run ID"},
            "group_key": {
                "type": "string",
                "required": True,
                "description": "Group key: root_cause:action:booking_vehicle",
            },
            "currency": {
                "type": "string",
                "required": False,
                "description": "Optional currency scope for multi-currency group_key collisions",
            },
            "notes": {"type": "string", "required": False, "description": "Optional note for the audit event"},
            "included_above_materiality_ids": {
                "type": "array",
                "required": False,
                "description": (
                    "Above-materiality proposal IDs to explicitly include — above-materiality "
                    "proposals never approve implicitly"
                ),
            },
            "excluded_ids": {
                "type": "array",
                "required": False,
                "description": "Proposal IDs to exclude from this batch",
            },
        },
    },
    "report.compose": {
        "description": (
            "Compose a publishable report (a summary + charts, NOT a raw data dump) from "
            "results already produced in this conversation. Pass title + ordered sections; "
            "valid section types: heading, narrative, metric_headline, chart, table, divider. "
            "Lead with narrative + metric_headline + a chart of the major drivers; include a "
            "raw table only when the detail is genuinely informative. Data sections reference "
            "a prior result by result_id (r1, r2, ... — stable per conversation, in the order "
            "the results were produced) — pass those EXACT ids (never inline numbers). "
            "Returns a report card; the report renders in the browser."
        ),
        "execute": report_export.execute,
        "params_schema": {
            "title": {"type": "string", "required": True, "description": "Report title"},
            "sections": {
                "type": "array",
                "required": True,
                "description": "Ordered report sections (see reporting profile)",
            },
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
            "Compute a sandbox-deploy preview and mint a one-shot HMAC token. "
            "Does NOT queue the deploy — the orchestrator will surface a "
            "confirmation_required event with the preview manifest, and the "
            "user must call workspace.deploy_sandbox_confirm with the returned "
            "jti + confirmation_token to actually run the deploy."
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
            "require_assertions": {
                "type": "boolean",
                "required": False,
                "default": False,
                "description": "Whether SuiteQL assertions must pass before deploy",
            },
        },
    },
    "workspace.deploy_sandbox_confirm": {
        "description": (
            "Confirm a previously minted sandbox-deploy preview token and "
            "queue the actual deploy run. Caller must supply the jti + "
            "confirmation_token from a workspace.deploy_sandbox preview "
            "response. Snapshot + gates are re-verified at confirm time; "
            "the worker re-verifies once more before invoking suitecloud "
            "project:deploy."
        ),
        "execute": workspace_tools.execute_deploy_sandbox_confirm,
        "params_schema": {
            "jti": {
                "type": "string",
                "required": True,
                "description": "Preview token id (jti) returned by workspace.deploy_sandbox",
            },
            "confirmation_token": {
                "type": "string",
                "required": True,
                "description": "HMAC confirmation token returned by workspace.deploy_sandbox",
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
                    "The Google Sheet ID. Extract from the URL: https://docs.google.com/spreadsheets/d/<ID>/edit"
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
    "drive.read_doc": {
        "description": (
            "Read the text content of a Google Drive file on demand. Accepts a "
            "Drive URL (https://docs.google.com/document/d/.../edit or "
            "https://drive.google.com/file/d/.../view) or a raw file ID. "
            "Supports Google Docs, PDFs, Google Sheets, Word (.docx), plain "
            "text, and Markdown. Use when the user pastes a Drive link and "
            "asks to summarize, extract, or reason about its contents. The "
            "service account must have access to the file (Shared Drive "
            "membership or explicit share). Returns text up to 50k chars "
            "plus source_name and web_view_link for citation."
        ),
        "execute": drive_tools.drive_read_doc_execute,
        "params_schema": {
            "file_id_or_url": {
                "type": "string",
                "required": True,
                "description": "Drive URL or raw file ID",
            },
        },
    },
    "docs.create": {
        "description": (
            "Create a new Google Doc from markdown content. Drive's markdown "
            "importer preserves formatting — headings, bold/italic, bullet "
            "and numbered lists, code blocks, blockquotes, and simple tables "
            "all convert to native Doc styling. The Doc is placed in the "
            "tenant's configured Shared Drive when set (or a specific folder "
            "via `folder_id`); otherwise it is created in the service "
            "account's Drive and auto-shared with the requesting user. Use "
            "when the user asks to save prose, research, notes, a brief, or "
            "a summary as a Google Doc. Returns a shareable URL that the "
            "frontend renders as a clickable card — do NOT paste the URL in "
            "your reply."
        ),
        "execute": docs_tools.docs_create_execute,
        "params_schema": {
            "title": {
                "type": "string",
                "required": True,
                "description": "Doc title (also becomes the filename in Drive).",
            },
            "content_markdown": {
                "type": "string",
                "required": True,
                "description": (
                    "Doc body as markdown. Headings (`#`, `##`, ...), bold "
                    "(`**x**`), italic (`*x*`), inline code, code blocks, "
                    "bullet and numbered lists, blockquotes, and simple "
                    "tables all convert to native Doc formatting."
                ),
            },
            "folder_id": {
                "type": "string",
                "required": False,
                "description": (
                    "Optional Drive folder ID to place the Doc in. Defaults to the connector's configured Shared Drive."
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
            "Pass updates as a dict with 'eur_fx_rate' and/or 'currencies' containing per-currency changes. "
            "Use top-level eur_fx_rate only for realistic USD-to-EUR exchange rates when the user explicitly "
            "asks to change the EUR base rate and recompute EUR-based currencies; use pricing_revise "
            "target_final_prices for final EUR display prices, and do not update currencies.EUR.fx_rate."
        ),
        "execute": pricing_tools.pricing_config_update_execute,
        "params_schema": {
            "updates": {
                "type": "object",
                "required": True,
                "description": (
                    "Fields to update. Example: {eur_fx_rate: 1.08, currencies: {GBP: {fx_rate: 0.79, vat_rate: 20}}}. "
                    "Use top-level eur_fx_rate for explicit EUR base-rate changes with realistic USD-to-EUR "
                    "exchange rates that should recompute EUR-based currencies. Use pricing_revise "
                    "target_final_prices for final EUR display prices, and do not use currencies.EUR.fx_rate."
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
    "pricing.to_sheets": {
        "description": (
            "Export the most recent pricing result to a new Google Sheet. Use when "
            "the user asks to export, share, or send pricing to a Sheet. Reads the "
            "cached pricing state server-side — does NOT need row data passed in. "
            "Returns the spreadsheet URL. The Excel + NetSuite CSV downloads from the "
            "prior pricing run are preserved separately."
        ),
        "execute": pricing_tools.pricing_to_sheets_execute,
        "params_schema": {
            "title": {
                "type": "string",
                "required": False,
                "description": "Optional spreadsheet title. Defaults to 'Pricing Export — <YYYY-MM-DD>'.",
            },
        },
    },
    "pricing.revise": {
        "description": (
            "Revise the most recent pricing result with the requested overrides and "
            "regenerate Excel + NetSuite CSV outputs. Supports SKU USD input prices, "
            "target final EUR display prices for a single-SKU pricing state, percent uplifts, "
            "rounding, configured FX overrides for USD-based currencies, VAT overrides, and currency/SKU "
            "add/remove edits. Use for follow-up edits like 'increase GBP by 5%', "
            "'set EUR to 149 and update EUR-based currencies', "
            "'change SKU ABC-123 USD to 149', 'use nearest_50 rounding for JPY only', "
            "'add EUR and CAD', 'remove SKU X'. Price outputs render automatically; "
            "assistant text must not list individual prices. "
            "DO NOT call this for the first pricing run — use pricing_convert (uploaded "
            "Excel) or pricing_export (inline items) instead. All numeric fields are "
            "interpreted with full Decimal precision; pass values as JSON numbers."
        ),
        "execute": pricing_tools.pricing_revise_execute,
        "params_schema": {
            "reset": {
                "type": "boolean",
                "required": False,
                "description": (
                    "If true, drop all accumulated overrides and revert to the original "
                    "seeded items + base config. Other override fields are ignored when reset=true."
                ),
            },
            "overrides": {
                "type": "object",
                "required": True,
                "description": (
                    "Changes to apply. All fields optional; combine freely. Supports: "
                    "sku_price_changes (list of {sku, usd_price}), skus_to_remove (list of "
                    "sku strings), skus_to_add (list of {sku, usd_price, item_name?}), "
                    "target_final_prices (dict by currency; currently supports {EUR: price} "
                    "for exactly one effective SKU and recomputes EUR-based currencies), "
                    "percent_uplift (dict by currency, e.g. {GBP: 0.05} = +5%), "
                    "configured fx_rate_overrides for USD-based currencies, "
                    "vat_rate_overrides (dict by currency), "
                    "rounding_overrides (dict by currency, e.g. {JPY: 'nearest_50'}), "
                    "currencies_to_add / currencies_to_remove (list of currency codes). "
                    "Do not use fx_rate_overrides for EUR-based currencies or to set "
                    "final displayed prices."
                ),
            },
        },
    },
    "metric.resolve": {
        "description": (
            "Resolve a named business metric from natural language or a key. Returns the blessed "
            "definition, params_schema, and dimensions. Definitions are DISPLAY-ONLY — never compute "
            "a number from them; call metric_compute to get a value."
        ),
        "execute": metric_tools.resolve,
        "params_schema": {
            "query": {"type": "string", "required": True, "description": "NL phrase or metric key"},
            "top_k": {"type": "integer", "required": False, "description": "max matches (default 5)"},
        },
    },
    "metric.compute": {
        "description": (
            "Deterministically compute a named metric by key with typed params (e.g. period token). "
            "Executes the blessed query/expression; the result renders as a table automatically."
        ),
        "execute": metric_tools.compute,
        "params_schema": {
            "key": {"type": "string", "required": True, "description": "metric key (from metric.resolve)"},
            "params": {
                "type": "object",
                "required": False,
                "description": 'typed params, e.g. {"period": "last_quarter"}',
            },
        },
    },
}
