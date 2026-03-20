---
name: netsuite-mcp
description: >
  NetSuite MCP Standard Tools — complete reference for using NetSuite's native MCP protocol tools
  (ns_runReport, ns_runSavedSearch, ns_listAllReports, ns_listSavedSearches, ns_runCustomSuiteQL,
  ns_getSuiteQLMetadata, ns_getSubsidiaries, ns_createRecord, ns_getRecord, ns_updateRecord,
  ns_getRecordTypeMetadata) in the SuiteStudio agent. Use this skill whenever working on the
  chat agent's tool routing, financial report pipeline, MCP connector integration, external tool
  definitions, orchestrator prompt injection, or agent system prompts. Also trigger when adding
  new MCP tools to the agent, debugging MCP tool execution, optimizing token usage for data
  queries, when the agent ignores available MCP tools, or when implementing CRUD/write operations.
  This skill is the authoritative guide for hybrid architecture — MCP for execution, backend for
  context intelligence. Covers tool visibility (role-permission based), Record Tools guardrails,
  and the complete permission matrix.
---

# NetSuite MCP Standard Tools — Complete Reference

## Philosophy: Hybrid Architecture (MCP + RAG + Local)

The agent's power comes from combining TWO systems:

1. **MCP tools** (execution) — Run reports, saved searches, and queries inside NetSuite.
   NetSuite handles authentication, accounting logic, sign conventions, and formatting natively.

2. **Backend context layer** (intelligence) — Entity resolution, tenant schema, RAG, learned rules,
   proven patterns, SuiteQL dialect rules. This is what makes the agent smart about the SPECIFIC
   tenant's business, not just a generic NetSuite query runner.

**The principle: MCP for execution, backend for context. Don't rebuild what NetSuite provides,
but don't lose the intelligence our backend adds.**

MCP tools know HOW to run a report. Our backend knows WHICH report to run, with WHAT filters,
for WHICH subsidiary — because it understands the tenant's vernacular, custom fields, and business rules.

## Tool Visibility: Role-Permission Based

**Critical concept:** Tool visibility is determined by the OAuth role's permissions, NOT the SuiteApp version.
A tool appears in `list_tools()` only if the role has ALL required permissions for that tool. Same SuiteApp
on two accounts can expose different tools if the OAuth roles have different permissions.

This means:
- If a tenant is missing Record Tools → their OAuth role lacks `REST Web Services (Full)` + record-type permissions
- If a tenant is missing Saved Search Tools → their OAuth role lacks `Perform Search (Full)`
- After updating role permissions, reconnect MCP to trigger `discover_tools()` refresh

### Permission Matrix for Complete MCP Access

| Permission | Tab | Level | Required For |
|-----------|-----|-------|-------------|
| MCP Server Connection | Setup | Full | Base MCP access |
| OAuth 2.0 Access Tokens | Setup | Full | OAuth authentication |
| REST Web Services | Setup | Full | Record Tools (CRUD) |
| Perform Search | Lists | Full | Saved Search Tools |
| SuiteQL | Setup | Full | SuiteQL Tools |
| Log In Using Access Tokens | Setup | Full | Token-based auth |
| Specific record types | Transactions/Lists | View/Create/Edit | Per-record CRUD |
| Financial reports | Reports | View | Report Tools |
| Subsidiaries | Lists | View | ns_getSubsidiaries |

**Important:** The Administrator role CANNOT be used — Oracle explicitly prohibits it for MCP.

## Available MCP Tools (~13 total, 4 categories)

Each tenant's connected NetSuite account exposes tools via the MCP Standard Tools SuiteApp
at `https://{account_id}.app.netsuite.com/services/mcp/v1/all`.

Which tools appear depends on the OAuth role's permissions (see above).

### 1. `ns_runReport` — Run Native NetSuite Reports

**Purpose:** Execute any NetSuite report (Income Statement, Balance Sheet, Trial Balance, A/R Aging,
A/P Aging, etc.) with native formatting, currency handling, and period filtering.

**When to use:** ANY financial statement or standard report request. This is the PRIMARY tool for
financial queries — not SuiteQL, not SQL templates.

**Parameters:**
```json
{
  "reportId": "string — internal ID of the report (get from ns_listAllReports)",
  "filters": {
    "period": "string — accounting period name or ID",
    "subsidiary": "string — subsidiary ID",
    "department": "string — department ID",
    "class": "string — class/channel ID",
    "location": "string — location ID"
  }
}
```

**Key behaviors:**
- Returns pre-formatted data with proper sign conventions (no manual negation needed)
- Handles multi-currency consolidation natively
- Respects NetSuite's accounting period calendar (no date math needed)
- Includes subtotals, section headers, and report structure
- Balance Sheet reports are inception-to-date (no start date needed)
- Income Statement reports use date ranges (start + end period)

**Common report types and their typical IDs:**
- Income Statement / P&L
- Balance Sheet
- Trial Balance
- A/R Aging
- A/P Aging
- Cash Flow Statement
- General Ledger

**Important:** Report IDs vary per NetSuite account. ALWAYS use `ns_listAllReports` first to
discover the correct IDs for the tenant. Never hardcode report IDs.

### 2. `ns_listAllReports` — Discover Available Reports

**Purpose:** List all reports available in the tenant's NetSuite account with their IDs and names.

**When to use:** Before calling `ns_runReport` for the first time, or when the user asks for a
report type you haven't cached the ID for.

**Parameters:** None required.

**Response:** Array of `{ id, name, category }` objects.

**Caching strategy:** Cache the report list per tenant after first discovery. Reports rarely change.
Store in `tenant_report_cache` or similar in-memory/Redis cache with 24hr TTL.

### 3. `ns_runSavedSearch` — Execute Saved Searches

**Purpose:** Run a NetSuite Saved Search by its internal ID. Saved Searches are pre-built queries
that NetSuite admins create for common data needs.

**When to use:**
- When the user references a specific saved search by name
- When a tenant has custom business logic encoded in saved searches
- For complex multi-join queries that would be error-prone in raw SuiteQL
- When the data need matches an existing saved search exactly

**Parameters:**
```json
{
  "savedSearchId": "string — internal ID of the saved search",
  "filters": [
    {
      "name": "string — filter field name",
      "operator": "string — e.g., 'is', 'after', 'between'",
      "values": ["string — filter values"]
    }
  ]
}
```

**Key behaviors:**
- Saved searches have their own column definitions — results may differ from raw SuiteQL
- They respect role-based permissions natively
- They may have formula columns, summary calculations, and grouping built in
- Runtime filters override the saved search's default filters

### 4. `ns_listSavedSearches` — Discover Saved Searches

**Purpose:** List all saved searches available in the tenant's NetSuite account.

**When to use:** When the user asks "do we have a saved search for X?" or when you need to
discover what pre-built queries exist before writing raw SuiteQL.

**Parameters:** May accept optional type filter (e.g., "transaction", "customer", "item").

**Response:** Array of saved search objects with IDs, names, and record types.

### 5. `ns_runCustomSuiteQL` — Execute Raw SuiteQL

**Purpose:** Run arbitrary SuiteQL queries directly inside NetSuite via MCP.

**When to use:**
- Ad-hoc data queries that don't map to a standard report or saved search
- Custom record queries (`customrecord_*`)
- Exploratory queries (count, sample, schema discovery)
- When `ns_runReport` and `ns_runSavedSearch` don't cover the need

**Parameters:**
```json
{
  "sqlQuery": "string — the SuiteQL query",
  "description": "string — brief description of what the query does"
}
```

**Governance:** Our backend automatically appends `FETCH FIRST 50 ROWS ONLY` if no pagination
clause is present. This prevents runaway queries.

**IMPORTANT:** All SuiteQL dialect rules from the `netsuite-mastery` skill still apply:
- No `LIMIT` keyword — use `FETCH FIRST N ROWS ONLY`
- Single-letter status codes via REST/MCP (not compound codes)
- `BUILTIN.DF()` for display names of list fields
- Item table silent 0-row problem still applies

### 6. `ns_getSuiteQLMetadata` — Schema Discovery

**Purpose:** Get metadata about available SuiteQL tables and columns directly from NetSuite.

**When to use:**
- When you need to verify column names before writing a query
- When the tenant schema in the system prompt doesn't cover a specific table
- When exploring unfamiliar custom records

**Parameters:** May accept table name filter.

### 7. `ns_getSubsidiaries` — Subsidiary Hierarchy

**Purpose:** Get the list of subsidiaries with their hierarchy and base currencies.

**When to use:**
- When filtering reports by subsidiary
- When the user mentions a subsidiary by name and you need the ID
- When generating consolidated vs. single-subsidiary reports

**Parameters:** None required.

### 8. `ns_getRecordTypeMetadata` — Record Type Discovery

**Purpose:** Get metadata for all NetSuite record types or a specific type, including available
fields, field types, and sublists.

**When to use:**
- Before calling `ns_createRecord` or `ns_updateRecord` — to know which fields exist
- When exploring what record types are available for CRUD
- When the user asks "what fields does a sales order have?"

**Parameters:**
```json
{
  "recordType": "string — optional, e.g. 'salesOrder', 'customer', 'journalEntry'"
}
```

**Requires:** REST Web Services (Full) permission on the OAuth role.

### 9. `ns_getRecord` — Retrieve a Record

**Purpose:** Retrieve a single NetSuite record by type and internal ID.

**When to use:**
- When the user asks to look up a specific record (customer, order, invoice)
- When you need to inspect a record before updating it
- When verifying that a created record exists

**Parameters:**
```json
{
  "recordType": "string — e.g. 'salesOrder', 'customer', 'invoice'",
  "id": "string — internal ID of the record"
}
```

**Requires:** REST Web Services (Full) + View permission on the specific record type.

### 10. `ns_createRecord` — Create a Record

**Purpose:** Create a new NetSuite record (customer, journal entry, invoice, etc.).

**When to use:**
- When the user explicitly asks to create a record in NetSuite
- When reconciliation finds a discrepancy that needs a journal entry
- When Shopify data needs to be pushed as transactions

**Parameters:**
```json
{
  "recordType": "string — e.g. 'journalEntry', 'customer', 'salesOrder'",
  "values": { "field": "value", ... },
  "sublists": { "sublistId": [{ "field": "value" }] }
}
```

**Requires:** REST Web Services (Full) + Create permission on the specific record type.

**GUARDRAILS (MANDATORY):**
1. **Always confirm with user** before creating any record
2. **Show the full payload** to the user before executing
3. **Log the creation** via audit_service with the returned internal ID
4. **Check record type allowlist** — only create approved record types per tenant config

### 11. `ns_updateRecord` — Update a Record

**Purpose:** Update an existing NetSuite record by type and internal ID.

**When to use:**
- When the user explicitly asks to modify a record
- When a correction needs to be applied (e.g., fix a wrong amount)
- NEVER for bulk updates without explicit user approval per record

**Parameters:**
```json
{
  "recordType": "string — e.g. 'salesOrder', 'customer'",
  "id": "string — internal ID of the record to update",
  "values": { "field": "newValue", ... }
}
```

**Requires:** REST Web Services (Full) + Edit permission on the specific record type.

**GUARDRAILS (MANDATORY):**
1. **Always retrieve the record first** (ns_getRecord) to show current state
2. **Show before/after diff** to the user
3. **Get explicit confirmation** before executing
4. **Log the update** with before/after values via audit_service
5. **Never update financial amounts** without extra confirmation threshold

## Tool Selection Decision Tree (Hybrid)

```
User asks a question about NetSuite data
│
├── ALWAYS FIRST: Check injected context
│   ├── <tenant_vernacular> → resolve entity names to internal IDs
│   ├── <tenant_schema> → verify custom fields exist
│   ├── <learned_rules> → check for tenant-specific preferences
│   └── <proven_patterns> → check for successful past queries
│
├── Financial statement? (P&L, Balance Sheet, Trial Balance, A/R Aging, etc.)
│   └── YES → ns_runReport (MCP) with IDs from entity resolution
│            → ns_listAllReports (MCP) first for report ID discovery
│            → FALLBACK: netsuite_financial_report (local SQL templates)
│            NEVER use raw SuiteQL for standard financial reports.
│
├── References a specific saved search or pre-built report?
│   └── YES → ns_runSavedSearch (MCP)
│            → ns_listSavedSearches (MCP) for discovery
│
├── Ad-hoc data query? (orders, items, customers, inventory)
│   └── YES → ns_runCustomSuiteQL (MCP, preferred) or netsuite_suiteql (local, fallback)
│            → USE <suiteql_dialect_rules> for both paths
│            → USE <tenant_schema> to verify column names BEFORE querying
│            → USE <tenant_vernacular> to resolve entities in WHERE clauses
│
├── Need to verify schema/columns?
│   └── Check <tenant_schema> first → netsuite_get_metadata (local, cached)
│       → ns_getSuiteQLMetadata (MCP, ground-truth) if local doesn't have it
│
├── Need to create/update a record? (journal entry, customer, order)
│   └── YES → ns_getRecordTypeMetadata (MCP) to discover fields
│            → ns_createRecord / ns_updateRecord (MCP) WITH GUARDRAILS:
│              1. Show payload to user, get explicit confirmation
│              2. Log action via audit_service
│              3. Check record type allowlist
│            → For updates: ns_getRecord first to show before/after diff
│            NOTE: Record Tools require REST Web Services (Full) permission
│
├── Need subsidiary info?
│   └── ns_getSubsidiaries (MCP)
│
└── Documentation / how-to / error lookup?
    └── rag_search (local) → web_search (local). No MCP needed.
```

## Agent Integration Architecture

### How MCP Tools Reach the Agent

1. **OAuth connection** → User authorizes NetSuite OAuth 2.0
2. **Tool discovery** → `mcp_client_service.discover_tools()` calls `session.list_tools()` on the MCP endpoint
3. **Storage** → Tools saved in `McpConnector.discovered_tools` JSON column
4. **Tool definitions** → `build_external_tool_definitions()` converts to Anthropic format
5. **Naming** → Each tool gets name `ext__{connector_id_hex}__{tool_name}` (max 64 chars)
6. **Execution** → `execute_tool_call()` detects `ext__` prefix → routes to `call_external_mcp_tool()`
7. **Auth** → `_build_headers()` auto-refreshes OAuth 2.0 token before each call

### Critical: Tool Guidance in System Prompt

The agent receives MCP tools in its tool list automatically, but **it won't use them properly
without explicit guidance in the system prompt**. The orchestrator MUST inject:

1. **Tool name mapping** — Tell the agent which `ext__...` name maps to which MCP tool
2. **When to use each tool** — Decision tree (see above)
3. **Parameter format** — Each tool's expected JSON parameters
4. **Priority order** — `ns_runReport` > `ns_runSavedSearch` > `ns_runCustomSuiteQL` > local `netsuite_suiteql`

### Current Orchestrator Injection (Lines 349-373)

The orchestrator currently only detects and promotes `ext__` tools containing "suiteql" in the name.
This MUST be expanded to detect and promote ALL MCP tools:

```python
# CURRENT (broken — only detects SuiteQL):
if td["name"].startswith("ext__") and "suiteql" in td["name"].lower():
    ext_suiteql_tools.append(td["name"])

# CORRECT (detect all MCP tools):
MCP_TOOL_PATTERNS = {
    "runreport": "FINANCIAL_REPORTS",
    "runsavedsearch": "SAVED_SEARCHES",
    "listallreports": "REPORT_DISCOVERY",
    "listsavedsearches": "SEARCH_DISCOVERY",
    "suiteql": "SUITEQL",
    "getsuiteqlmetadata": "METADATA",
    "getsubsidiaries": "SUBSIDIARIES",
    "createrecord": "RECORD_CREATE",
    "getrecord": "RECORD_GET",
    "updaterecord": "RECORD_UPDATE",
    "getrecordtypemetadata": "RECORD_METADATA",
}

ext_mcp_tools = {}  # category → tool_name
for td in tool_definitions:
    if td["name"].startswith("ext__"):
        lower_name = td["name"].lower()
        for pattern, category in MCP_TOOL_PATTERNS.items():
            if pattern in lower_name:
                ext_mcp_tools[category] = td["name"]
```

Then inject a comprehensive guidance block:

```python
if ext_mcp_tools:
    guidance = ["\n\nNETSUITE MCP TOOLS (prefer these over local tools):"]

    if "FINANCIAL_REPORTS" in ext_mcp_tools:
        guidance.append(
            f"\n• FINANCIAL REPORTS: Use `{ext_mcp_tools['FINANCIAL_REPORTS']}` for ALL "
            "financial statements (P&L, Balance Sheet, Trial Balance, Aging). "
            "Parameters: {{\"reportId\": \"...\", \"filters\": {{...}}}}"
        )

    if "REPORT_DISCOVERY" in ext_mcp_tools:
        guidance.append(
            f"\n• DISCOVER REPORTS: Use `{ext_mcp_tools['REPORT_DISCOVERY']}` to list all "
            "available reports and find the correct reportId."
        )

    if "SAVED_SEARCHES" in ext_mcp_tools:
        guidance.append(
            f"\n• SAVED SEARCHES: Use `{ext_mcp_tools['SAVED_SEARCHES']}` to run "
            "pre-built saved searches. Parameters: {{\"savedSearchId\": \"...\"}}"
        )

    if "SUITEQL" in ext_mcp_tools:
        guidance.append(
            f"\n• SUITEQL (MCP): Use `{ext_mcp_tools['SUITEQL']}` for ad-hoc queries. "
            "Prefer over local netsuite_suiteql. "
            "Parameters: {{\"sqlQuery\": \"SELECT ...\", \"description\": \"...\"}}"
        )

    if "RECORD_CREATE" in ext_mcp_tools or "RECORD_UPDATE" in ext_mcp_tools:
        guidance.append(
            f"\n• RECORD TOOLS: "
            + (f"Create: `{ext_mcp_tools.get('RECORD_CREATE', 'N/A')}` " if "RECORD_CREATE" in ext_mcp_tools else "")
            + (f"Read: `{ext_mcp_tools.get('RECORD_GET', 'N/A')}` " if "RECORD_GET" in ext_mcp_tools else "")
            + (f"Update: `{ext_mcp_tools.get('RECORD_UPDATE', 'N/A')}` " if "RECORD_UPDATE" in ext_mcp_tools else "")
            + "GUARDRAILS: Always show payload + get user confirmation before create/update."
        )

    if "RECORD_METADATA" in ext_mcp_tools:
        guidance.append(
            f"\n• RECORD METADATA: Use `{ext_mcp_tools['RECORD_METADATA']}` to discover "
            "record types and fields before creating/updating records."
        )

    guidance.append(
        "\n\nPRIORITY: ns_runReport → ns_runSavedSearch → ns_runCustomSuiteQL → netsuite_suiteql"
        "\nFor CRUD: ns_getRecordTypeMetadata → ns_getRecord (read) → ns_createRecord / ns_updateRecord (with confirmation)"
    )
    tool_inventory_lines.extend(guidance)
```

### Token Budget Per Query Type

| Query Type | Context Injected | Tokens |
|-----------|-----------------|--------|
| Financial statement | entity resolution + learned rules + soul + MCP guidance | ~2,000 |
| Ad-hoc SuiteQL | FULL (schema, vernacular, patterns, rules, dialect) + MCP guidance | ~8,000-12,000 |
| Documentation | entity resolution + soul + MCP guidance | ~2,000 |
| Workspace/code | entity resolution + soul + MCP guidance | ~2,000 |

Financial queries: 40-50K → ~2K (skip domain knowledge + table schemas, keep entity resolution).
SuiteQL queries: unchanged (they NEED schema and dialect rules — that's our backend's value).

### Fallback Chain

If an MCP tool fails (timeout, auth error, missing SuiteApp):

1. `ns_runReport` fails → fall back to `ns_runSavedSearch` (if a matching saved search exists)
2. `ns_runSavedSearch` fails → fall back to `ns_runCustomSuiteQL` (raw SuiteQL via MCP)
3. `ns_runCustomSuiteQL` fails → fall back to local `netsuite_suiteql` (REST API via backend)
4. `netsuite_suiteql` fails → fall back to `netsuite_financial_report` (legacy SQL templates)

The legacy `netsuite_financial_report` tool should be kept but deprioritized. Remove it from
the default tool set and only surface it if all MCP paths fail.

## Common Mistakes to Avoid

1. **Don't hardcode report IDs** — They vary per NetSuite account. Always discover via `ns_listAllReports`.
2. **Don't build SQL for standard reports** — If NetSuite has a native report for it, use `ns_runReport`.
3. **Don't only detect "suiteql" in ext__ names** — All ~13 MCP tools need to be detected and promoted.
4. **Don't dump raw MCP results into the prompt** — Let the agent call the tool and process the response naturally.
5. **Don't use tool_choice forcing** — If the prompt guidance is clear, the LLM will pick the right tool.
6. **Don't forget the 15-second timeout** — MCP calls timeout at 15s. Large reports may need pagination.
7. **Don't skip OAuth token refresh** — The `_build_headers()` function handles this, but if a connector
   shows `status: "error"`, the user needs to re-authorize.
8. **Don't ignore the 64-char tool name limit** — `_make_ext_tool_name()` truncates. Verify the agent
   sees the correct name in the tool inventory.
9. **Don't add MCP guidance as a one-off patch** — It should be part of the standard tool inventory
   injection in the orchestrator, not a separate prompt section.
10. **Don't forget to update ALLOWED_CHAT_TOOLS** — External tools bypass this list (they use the
    `ext__` prefix routing), but if you create local wrappers, add them to the allowlist.
11. **Don't assume all tenants have the same tools** — Tool visibility is role-permission based.
    A tenant missing Record Tools just needs `REST Web Services (Full)` + record-type permissions added
    to their OAuth role. Don't treat missing tools as a SuiteApp bug.
12. **Don't allow CRUD without confirmation** — Record Tools (create/update) MUST show the payload
    to the user and get explicit approval before execution. No auto-create, no auto-update.
13. **Don't skip ns_getRecord before ns_updateRecord** — Always fetch the current state first to
    show the user a before/after diff. This prevents blind overwrites.
14. **Don't forget record type allowlists** — Not every record type should be writable. Restrict
    CRUD to approved types per tenant (configurable in tenant settings).

## Files to Modify for MCP-First Migration

| File | What to change |
|------|---------------|
| `backend/app/services/chat/orchestrator.py` L349-373 | Expand ext__ detection to all MCP tool patterns |
| `backend/app/services/chat/orchestrator.py` L489-720 | Remove pre-execution block, remove financial mode JSON dump |
| `backend/app/services/chat/agents/unified_agent.py` L96-114 | Update <tool_selection> to include MCP tools |
| `backend/app/services/chat/agents/unified_agent.py` L38-41 | Update _FINANCIAL_TOOL_NAMES to include ext__ report tools |
| `backend/app/services/chat/prompts.py` L306-311 | Update prompt templates to MCP-first guidance |
| `backend/app/mcp/tools/netsuite_financial_report.py` | Keep as legacy fallback, deprioritize |
| `backend/app/services/mcp_client_service.py` L158-164 | Add governance intercepts for ns_runReport (timeout, caching) |

## Testing Checklist

### Read Tools
- [ ] `ns_listAllReports` returns report list for connected tenant
- [ ] `ns_runReport` with Income Statement ID returns formatted P&L
- [ ] `ns_runReport` with Balance Sheet ID returns formatted BS
- [ ] `ns_runSavedSearch` executes a known saved search by ID
- [ ] `ns_listSavedSearches` returns available saved searches
- [ ] Agent correctly selects `ns_runReport` when asked "show me the income statement"
- [ ] Agent falls back to SuiteQL when MCP report tool is unavailable
- [ ] Agent doesn't hallucinate report IDs (uses discovery first)
- [ ] Token count for financial query is under 1,000 (excluding response)
- [ ] OAuth token refresh works when token expires mid-session

### Record Tools (CRUD)
- [ ] `ns_getRecordTypeMetadata` lists available record types
- [ ] `ns_getRecordTypeMetadata` with specific type returns field metadata
- [ ] `ns_getRecord` retrieves a known record by type + ID
- [ ] `ns_createRecord` creates a test record (with user confirmation guardrail)
- [ ] `ns_updateRecord` updates a test record (with before/after diff shown)
- [ ] Agent shows confirmation dialog before any create/update
- [ ] Agent logs CRUD actions via audit_service
- [ ] Agent refuses CRUD on record types not in allowlist
- [ ] Missing Record Tools gracefully detected (not all tenants have them)

### Tool Visibility
- [ ] Tenant with REST Web Services (Full) sees Record Tools
- [ ] Tenant without REST Web Services sees only read tools
- [ ] Reconnecting MCP after role change refreshes discovered tools
- [ ] Orchestrator handles variable tool sets per tenant (no crashes on missing tools)
