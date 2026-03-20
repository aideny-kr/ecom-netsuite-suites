# Holistic Agent Revamp — MCP + RAG + SuiteQL Hybrid

## Read First (DO NOT SKIP)

1. **`skills/netsuite-mcp/SKILL.md`** — All 7 NetSuite MCP tools, parameters, decision tree.
2. **`backend/app/services/chat/orchestrator.py`** — Lines 349-375 (broken ext__ detection), 490-620 (context assembly), 630-634 (financial mode task).
3. **`backend/app/services/chat/agents/unified_agent.py`** — Lines 64-250+ (system prompt with `<tool_selection>`, `<suiteql_dialect_rules>`, `<how_to_think>`).
4. **`backend/app/services/chat/tools.py`** — How external MCP tools are surfaced via `build_external_tool_definitions()`.
5. **`CLAUDE.md`** — Project architecture and patterns.

## The Problem

The agent has TWO superpowers, but only uses one:

1. **MCP tools** (7 tools from NetSuite, already in the tool list) — native reports, saved searches, SuiteQL, metadata, subsidiaries. The agent ignores 6 of these because the orchestrator only tells it about the SuiteQL one.

2. **Backend context layer** (entity resolution, tenant schema, RAG, learned rules, proven patterns, domain knowledge) — the intelligence that makes this more than a raw MCP passthrough. Without this, the agent has no idea that "FW" means "Furniture Warehouse" subsidiary, or that `custbody_channel` is the sales channel field, or that the tenant uses "SO" to mean Sales Order.

The goal isn't MCP-only. It's a **smart hybrid** where:
- MCP handles EXECUTION (running reports, saved searches, queries inside NetSuite)
- Our backend handles CONTEXT (resolving entities, injecting tenant schema, RAG for docs, learned rules)
- The agent orchestrates both, picking the right tool for the job

## Architecture: Three Layers

```
┌─────────────────────────────────────────────────────────────────┐
│                     CONTEXT LAYER (our backend)                 │
│  Always active. Enriches every query with tenant intelligence.  │
│                                                                 │
│  • Entity resolution (vernacular → internal IDs via pg_trgm)   │
│  • Tenant schema (custom fields, record types, segments)        │
│  • Proven query patterns (successful SuiteQL from history)      │
│  • Learned rules (tenant-specific business rules)               │
│  • Soul config (tone, netsuite quirks, bot personality)         │
│  • Domain knowledge (RAG chunks — docs, golden dataset)         │
│  • Table schema injection (relevant columns for the query)      │
│                                                                 │
│  SKIP for financial reports: domain knowledge, table schemas    │
│  KEEP for financial reports: entity resolution, learned rules   │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│                   EXECUTION LAYER (MCP + local)                 │
│  Agent picks the best tool based on intent + context.           │
│                                                                 │
│  Priority for financial statements:                             │
│    1. ns_runReport (MCP) — native NetSuite report engine        │
│    2. ns_runSavedSearch (MCP) — pre-built tenant reports        │
│    3. netsuite_financial_report (local) — SQL template fallback │
│                                                                 │
│  Priority for ad-hoc data queries:                              │
│    1. ns_runCustomSuiteQL (MCP) — runs inside NetSuite          │
│    2. netsuite_suiteql (local) — REST API fallback              │
│    Both use the SAME tenant context: schema, vernacular, rules  │
│                                                                 │
│  Priority for discovery:                                        │
│    • ns_listAllReports (MCP) — find report IDs                  │
│    • ns_listSavedSearches (MCP) — find saved search IDs         │
│    • ns_getSuiteQLMetadata (MCP) — schema from NetSuite         │
│    • netsuite_get_metadata (local) — our cached tenant metadata │
│    • ns_getSubsidiaries (MCP) — subsidiary hierarchy            │
│                                                                 │
│  For docs/knowledge:                                            │
│    • rag_search (local) — docs, scripts, custom field metadata  │
│    • web_search (local) — NetSuite API reference, SuiteQL help  │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│                   KNOWLEDGE LAYER (our backend)                 │
│  For questions MCP can't answer.                                │
│                                                                 │
│  • RAG search — documentation, SuiteScript source, how-to      │
│  • Web search — NetSuite API docs, community answers            │
│  • Workspace tools — file ops, code review, patch proposals     │
│  • Learned rules — persistent tenant-specific corrections       │
└─────────────────────────────────────────────────────────────────┘
```

## What MCP Does Well (Let It)

| Capability | MCP Tool | Why better than our code |
|-----------|----------|------------------------|
| Financial statements | `ns_runReport` | NetSuite handles sign conventions, consolidation, elimination, multi-book, period calendar |
| Saved searches | `ns_runSavedSearch` | Pre-built by NetSuite admin with custom formulas, grouping, filters |
| Report discovery | `ns_listAllReports` | Always up-to-date, no hardcoded IDs |
| Search discovery | `ns_listSavedSearches` | Finds custom reports the admin created |
| SuiteQL execution | `ns_runCustomSuiteQL` | Runs inside NetSuite, fewer network hops |
| Schema from source | `ns_getSuiteQLMetadata` | Ground truth from NetSuite, not our cache |
| Subsidiary hierarchy | `ns_getSubsidiaries` | Always current, includes base currencies |

## What Our Backend Does Well (Don't Lose It)

| Capability | Our Tool/Service | Why MCP can't do this |
|-----------|-----------------|----------------------|
| Entity resolution | `TenantEntityResolver` | "FW" → subsidiary ID, "last month orders" → date range, NER + pg_trgm |
| Custom field awareness | `tenant_schema` injection | Pre-compiled from metadata discovery, knows `custbody_channel` etc. |
| Anti-hallucination | Schema validation, judge verdicts | Prevents wrong column names, catches bad SuiteQL |
| SuiteQL dialect rules | System prompt `<suiteql_dialect_rules>` | FETCH not LIMIT, single-letter status codes, BUILTIN.DF() |
| Proven patterns | `retrieve_similar_patterns()` | Successful past queries for similar questions |
| Domain knowledge (RAG) | `retrieve_domain_knowledge()` | Golden dataset: financial templates, status code docs |
| Learned rules | `tenant_save_learned_rule` | Persistent business rules the user taught the agent |
| Documentation | `rag_search` | SuiteScript source code, custom field docs, API reference |
| Workspace ops | `workspace_*` tools | File editing, code review, patch proposals |
| Soul config | `soul_config` injection | Tenant personality, netsuite quirks, preferred tone |

## The Changes (Four Modifications, Zero New Files)

### Change 1: Fix ext__ MCP tool detection in orchestrator

**File:** `backend/app/services/chat/orchestrator.py` lines 349-375

The orchestrator only detects "suiteql" in `ext__` tool names. Expand to detect all 7 MCP tools and inject guidance for each.

**Replace the current detection + guidance block with:**

```python
# Detect ALL NetSuite MCP tools by pattern matching the tool name
_MCP_TOOL_PATTERNS = {
    "runreport": "REPORTS",
    "runsavedsearch": "SAVED_SEARCHES",
    "listallreports": "REPORT_DISCOVERY",
    "listsavedsearches": "SEARCH_DISCOVERY",
    "suiteql": "SUITEQL",
    "getsuiteqlmetadata": "METADATA",
    "getsubsidiaries": "SUBSIDIARIES",
}

ext_mcp_tools: dict[str, str] = {}  # category → tool_name
for td in tool_definitions:
    tool_inventory_lines.append(f"- {td['name']}: {td.get('description', '')}")
    if td["name"].startswith("ext__"):
        lower_name = td["name"].lower()
        for pattern, category in _MCP_TOOL_PATTERNS.items():
            if pattern in lower_name:
                ext_mcp_tools[category] = td["name"]

if ext_mcp_tools:
    guidance = [
        "\n\nNETSUITE MCP TOOLS (connect directly to NetSuite — prefer these for execution):",
    ]

    if "REPORTS" in ext_mcp_tools:
        guidance.append(
            f"\n• FINANCIAL REPORTS: `{ext_mcp_tools['REPORTS']}`"
            "\n  For Income Statement, Balance Sheet, Trial Balance, Aging, GL, etc."
            '\n  Parameters: {"reportId": "<id>", "filters": {"period": "...", "subsidiary": "..."}}'
            "\n  → Get reportId by calling the report discovery tool first."
            "\n  → Balance Sheet = inception-to-date (no start date)."
            "\n  → NetSuite handles sign conventions, consolidation, currency natively."
        )

    if "REPORT_DISCOVERY" in ext_mcp_tools:
        guidance.append(
            f"\n• DISCOVER REPORTS: `{ext_mcp_tools['REPORT_DISCOVERY']}`"
            "\n  Lists all available reports with IDs. Call FIRST before ns_runReport."
        )

    if "SAVED_SEARCHES" in ext_mcp_tools:
        guidance.append(
            f"\n• SAVED SEARCHES: `{ext_mcp_tools['SAVED_SEARCHES']}`"
            "\n  Run pre-built searches with custom columns, formulas, and filters."
            '\n  Parameters: {"savedSearchId": "<id>", "filters": [...]}'
        )

    if "SEARCH_DISCOVERY" in ext_mcp_tools:
        guidance.append(
            f"\n• DISCOVER SEARCHES: `{ext_mcp_tools['SEARCH_DISCOVERY']}`"
            "\n  Lists saved searches. Use when user asks 'do we have a report for X?'"
        )

    if "SUITEQL" in ext_mcp_tools:
        guidance.append(
            f"\n• SUITEQL (MCP): `{ext_mcp_tools['SUITEQL']}`"
            "\n  Ad-hoc SuiteQL queries inside NetSuite. Prefer over local netsuite_suiteql."
            '\n  Parameters: {"sqlQuery": "SELECT ...", "description": "..."}'
            "\n  STILL FOLLOW all <suiteql_dialect_rules> — they apply to MCP SuiteQL too."
        )

    if "METADATA" in ext_mcp_tools:
        guidance.append(
            f"\n• SCHEMA (MCP): `{ext_mcp_tools['METADATA']}`"
            "\n  Ground-truth column metadata from NetSuite. Use alongside netsuite_get_metadata."
        )

    if "SUBSIDIARIES" in ext_mcp_tools:
        guidance.append(
            f"\n• SUBSIDIARIES: `{ext_mcp_tools['SUBSIDIARIES']}`"
            "\n  Subsidiary hierarchy with base currencies."
        )

    guidance.append(
        "\n\nEXECUTION PRIORITY (pick the first that fits):"
        "\n  Financial statements → ns_runReport"
        "\n  Pre-built business reports → ns_runSavedSearch"
        "\n  Ad-hoc data queries → ns_runCustomSuiteQL (MCP) → netsuite_suiteql (local fallback)"
        "\n  Schema verification → ns_getSuiteQLMetadata + netsuite_get_metadata (use both)"
        "\n  Documentation/how-to → rag_search → web_search"
        "\n"
        "\nIMPORTANT: MCP tools handle EXECUTION. But you still have rich tenant context"
        "\n(entity vernacular, custom field schema, learned rules, proven patterns) injected"
        "\ninto your system prompt. USE THIS CONTEXT when constructing parameters for MCP tools."
        "\nFor example, if <tenant_vernacular> resolves 'FW' to subsidiary ID 5, pass"
        "\nsubsidiaryId: 5 to ns_runReport."
    )

    tool_inventory_lines.append("\n".join(guidance))
```

### Change 2: Update unified agent's `<tool_selection>` block

**File:** `backend/app/services/chat/agents/unified_agent.py` lines 96-114

Replace the `<tool_selection>` block. The key difference from the old version: it explains the HYBRID approach — MCP for execution, backend context for intelligence.

```
<tool_selection>
CHOOSE THE RIGHT TOOL — HYBRID APPROACH:

You have TWO types of tools:
  • MCP tools (ext__... prefixed) — execute directly inside NetSuite. Best for data retrieval.
  • Local tools — our backend tools. Best for context, docs, workspace, and fallback.

Use BOTH together. MCP tools run the query; your injected context (<tenant_vernacular>,
<tenant_schema>, <proven_patterns>, <learned_rules>) tells you HOW to construct the query.

FINANCIAL STATEMENTS (P&L, Balance Sheet, Trial Balance, Aging, GL):
→ Use MCP ns_runReport. Call ns_listAllReports first to discover the report ID.
→ Use <tenant_vernacular> to resolve subsidiary names, department names to IDs for filters.
→ NEVER write raw SuiteQL for standard financial reports.
→ FALLBACK: netsuite_financial_report (local SQL templates) if MCP reports unavailable.

PRE-BUILT BUSINESS REPORTS:
→ Use MCP ns_runSavedSearch. Call ns_listSavedSearches to discover what's available.
→ Saved searches have custom columns and filters built by the NetSuite admin.
→ Great for tenant-specific reports that don't map to standard financial statements.

AD-HOC DATA QUERIES (orders, invoices, customers, items, inventory):
→ Use MCP ns_runCustomSuiteQL (preferred) or local netsuite_suiteql (fallback).
→ CHECK <tenant_schema> and <standard_table_schemas> for valid column names BEFORE querying.
→ CHECK <tenant_vernacular> to resolve entity names to internal IDs.
→ CHECK <proven_patterns> — if a similar query succeeded before, use its pattern.
→ CHECK <learned_rules> — tenant may have standing rules (e.g., "exclude cancelled orders").
→ FOLLOW ALL <suiteql_dialect_rules> — they apply to BOTH MCP and local SuiteQL.

SCHEMA/COLUMN DISCOVERY:
→ First check <tenant_schema> and <standard_table_schemas> (already in your context).
→ If a column is not listed, use netsuite_get_metadata (local, cached) for quick lookup.
→ Use MCP ns_getSuiteQLMetadata for ground-truth verification from NetSuite itself.
→ NEVER guess column names — wrong columns cause 400 errors or silent 0-row results.

DOCUMENTATION / HOW-TO / ERROR LOOKUPS:
→ rag_search first (internal docs, custom field metadata, SuiteScript source code).
→ web_search as fallback for NetSuite API reference, SuiteQL syntax, community answers.

WORKSPACE / CODE TASKS:
→ workspace_list_files, workspace_read_file, workspace_search, workspace_propose_patch.
→ Always read the target file before proposing changes.

LEARNING / CORRECTIONS:
→ tenant_save_learned_rule when the user gives a standing instruction or correction.
</tool_selection>
```

### Change 3: Update financial mode task to reference MCP but preserve context

**File:** `backend/app/services/chat/orchestrator.py` lines 32-44

```python
def _build_financial_mode_task(user_message: str) -> str:
    """Build task for financial report queries.

    Directs the agent to use MCP native reports while still leveraging
    tenant context (vernacular, learned rules) for parameter resolution.
    """
    return (
        f"{user_message}\n\n"
        "[FINANCIAL REPORT]\n"
        "Use NetSuite's native MCP report tools:\n"
        "1. Call ns_listAllReports to find the right report ID\n"
        "2. Call ns_runReport with that ID and appropriate period/filters\n"
        "3. Use <tenant_vernacular> to resolve entity names to IDs for filters\n"
        "4. Check <learned_rules> for any tenant-specific reporting preferences\n"
        "5. Present results in a clear, formatted table with sections and totals.\n"
        "Do NOT write raw SuiteQL for this — use NetSuite's native report engine.\n"
        "FALLBACK: If MCP report tools error, use netsuite_financial_report (local)."
    )
```

### Change 4: Keep entity resolution + learned rules for financial queries

**File:** `backend/app/services/chat/orchestrator.py` around line 564

The orchestrator currently skips domain knowledge and table schemas for financial queries (good — these are unnecessary for MCP reports). But verify it KEEPS:

- Entity resolution (vernacular) — needed to resolve "FW" → subsidiary ID 5
- Learned rules — needed for tenant-specific preferences ("always show in USD")
- Soul config — tone and netsuite quirks
- Proven patterns — optional but useful

**Current code (verify, don't change if correct):**
```python
if is_financial:
    context["domain_knowledge"] = []   # ✅ Skip — MCP handles the data
# But entity resolution still runs (line 532) ✅ KEEP
# And proven patterns still injected (line 577) ✅ KEEP
# And schema injection skipped (line 587) ✅ Skip — MCP handles column names
```

This is already correct. Just verify it stays this way.

## What NOT to Change

The agent's backend intelligence is a competitive advantage over raw MCP. Preserve all of:

| Keep | Why |
|------|-----|
| `<suiteql_dialect_rules>` | SuiteQL rules apply to MCP SuiteQL too (FETCH not LIMIT, single-letter status codes) |
| `<tenant_schema>` injection | Agent needs to know custom fields exist when building SuiteQL |
| Entity resolution | Agent needs to resolve "FW" → subsidiary 5 for MCP report filters |
| `<proven_patterns>` | Successful past queries help the agent avoid known pitfalls |
| `<learned_rules>` | Tenant-specific corrections persist across sessions |
| Anti-hallucination guards | Schema validation and judge verdicts still protect SuiteQL queries |
| `netsuite_financial_report` (SQL templates) | Fallback for accounts without MCP Standard Tools SuiteApp |
| `netsuite_suiteql` (local REST API) | Fallback when MCP SuiteQL tool errors or times out |
| RAG search + web search | MCP has zero documentation capability |
| Workspace tools | MCP has zero code/file capability |

## Testing — Hybrid Scenarios

Test cases that verify BOTH MCP execution and backend context work together:

1. **"Show me FW's income statement for February"**
   - Entity resolution resolves "FW" → subsidiary ID 5
   - Agent calls `ns_listAllReports` → gets income statement report ID
   - Agent calls `ns_runReport` with `reportId` + `subsidiaryId: 5`
   - **Tests:** MCP execution + entity resolution working together

2. **"Run the open PO report"**
   - Agent calls `ns_listSavedSearches` → finds "Open Purchase Orders" saved search
   - Agent calls `ns_runSavedSearch` with that ID
   - **Tests:** Saved search discovery + execution

3. **"How many orders for Hightower last week?"**
   - Entity resolution resolves "Hightower" → customer ID from `tenant_entity_mapping`
   - Agent calls `ns_runCustomSuiteQL` with SuiteQL filtering by that customer ID
   - SuiteQL uses FETCH FIRST (not LIMIT), single-letter status codes
   - **Tests:** Entity resolution + MCP SuiteQL + dialect rules

4. **"What custom fields are on sales orders?"**
   - Agent checks `<tenant_schema>` first (injected context)
   - If not sufficient, calls `netsuite_get_metadata` (local cached)
   - Optionally calls `ns_getSuiteQLMetadata` (MCP ground-truth)
   - **Tests:** Schema discovery from multiple sources

5. **"Show me the P&L but always exclude intercompany eliminations"**
   - Agent calls `ns_runReport` for P&L
   - Agent calls `tenant_save_learned_rule` to persist the exclusion preference
   - Next time user asks for P&L, `<learned_rules>` should remind agent to exclude eliminations
   - **Tests:** MCP execution + learning + persistence

6. **"What does the ecom_file_cabinet_restlet do?"**
   - Agent calls `rag_search` (documentation lookup)
   - Should NOT call any MCP tools — this is a knowledge question
   - **Tests:** RAG search for docs, no unnecessary MCP calls

7. **"Show orders shipped last week" (when MCP SuiteQL errors)**
   - Agent tries `ns_runCustomSuiteQL` (MCP) → gets timeout/error
   - Agent falls back to `netsuite_suiteql` (local REST API)
   - Uses `<standard_table_schemas>` to pick correct columns
   - **Tests:** Graceful fallback from MCP to local

8. **"Compare last month's P&L to the previous month"**
   - Agent calls `ns_runReport` twice (two different periods)
   - Formats comparison table with variance analysis
   - **Tests:** Multi-call MCP execution + analysis

## Token Budget (Per Query Type)

| Query Type | Context Injected | Est. Tokens |
|-----------|-----------------|-------------|
| Financial statement | entity resolution + learned rules + soul config + MCP guidance | ~2,000 |
| Ad-hoc SuiteQL | FULL context (schema, vernacular, patterns, rules, dialect) + MCP guidance | ~8,000-12,000 |
| Documentation | entity resolution + soul config + MCP guidance | ~2,000 |
| Workspace/code | entity resolution + soul config + MCP guidance | ~2,000 |

Financial queries drop from 40-50K to ~2K. Ad-hoc SuiteQL queries stay similar (they NEED the schema and dialect rules). This is the right tradeoff.

## DO NOT

- Do NOT create new service files — the MCP client and all context services already exist
- Do NOT create new tool registrations — external tools are auto-registered via discovery
- Do NOT delete any existing tools — they're all fallbacks for when MCP tools are unavailable
- Do NOT remove `<suiteql_dialect_rules>` — they apply to MCP SuiteQL too
- Do NOT remove entity resolution for financial queries — needed for filter parameter resolution
- Do NOT remove anti-hallucination guards — they protect SuiteQL queries regardless of execution path
- Do NOT add `tool_choice` forcing — the prompt guidance + tool inventory is sufficient
- Do NOT add pre-execution (data fetching before agent runs) — the agent calls tools itself
- Do NOT remove the `netsuite_financial_report` SQL template tool — it's the fallback chain's last resort
