"""Unified specialist agent — single agent with all tools.

Replaces the multi-agent routing architecture (coordinator → classify → route → specialist → synthesize)
with a single agent that has access to all tools and lets the LLM decide which to use.

Dynamic context (entity vernacular, domain knowledge, soul quirks, proven patterns)
is assembled before the agent runs and injected into the system prompt.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import TYPE_CHECKING, Any, Callable

from app.services.chat.agents.base_agent import BaseSpecialistAgent
from app.services.chat.tools import build_local_tool_definitions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.netsuite_metadata import NetSuiteMetadata
    from app.models.policy_profile import PolicyProfile
    from app.services.chat.llm_adapter import BaseLLMAdapter

_logger = logging.getLogger(__name__)

# Keywords that indicate the user's query is about scripts/automation/workflows.
_SCRIPT_KEYWORDS = re.compile(
    r"\b(?:scripts?|deploy(?:ment)?s?|workflows?|triggers?|automation|scheduled|user\s*events?|"
    r"suitelets?|restlets?|map\s*reduce|client\s*scripts?|mass\s*updates?|portlets?|"
    r"bundles?|sdf|customscript\w*)\b",
    re.IGNORECASE,
)

# Financial-mode tools — only these are exposed when handling financial queries
_FINANCIAL_TOOL_NAMES = frozenset({
    "netsuite_report",
    "rag_search",
})

# Superset of all specialist tools
_UNIFIED_TOOL_NAMES = frozenset(
    {
        # SuiteQL agent tools
        "netsuite_suiteql",
        "netsuite_get_metadata",
        "netsuite_financial_report",
        # RAG agent tools
        "rag_search",
        "web_search",
        # Workspace agent tools
        "workspace_list_files",
        "workspace_read_file",
        "workspace_search",
        "workspace_propose_patch",
        # Shared
        "tenant_save_learned_rule",
    }
)


_SYSTEM_PROMPT = """\
<role>
You are an expert NetSuite AI assistant. You combine deep knowledge of SuiteQL (Oracle-based SQL dialect), \
NetSuite documentation, SuiteScript development, and data analysis. Your job is to understand what the user \
needs and use the right tools to get the answer efficiently.
</role>

<tenant_context>
Below is the pre-compiled schema for this specific NetSuite tenant. Use this to immediately identify custom \
fields and active reporting segments without needing to call the metadata tool.
<tenant_schema>
{{INJECT_METADATA_HERE}}
</tenant_schema>
</tenant_context>

{{INJECT_TABLE_SCHEMAS}}

<how_to_think>
Before taking ANY action, reason through these steps in a <reasoning> block:
1. **ANTI-HALLUCINATION**: ONLY use columns listed in <standard_table_schemas> or <tenant_schema>. \
If a column is NOT listed, call netsuite_get_metadata to verify it exists BEFORE using it. \
NEVER guess or invent column names. For custom fields (custbody_*, custcol_*, custentity_*, custitem_*), verify in <tenant_schema>.
2. **NEVER COPY QUERIES FROM HISTORY**: When the user says "try again", "redo", or asks a follow-up, \
always construct a NEW query following <suiteql_dialect_rules>. \
Prior queries may have used wrong syntax (e.g. compound status codes). System prompt rules ALWAYS override conversation history.
3. **Native fields first**: Always check standard NetSuite fields and records before looking at custom fields \
(custbody_*, custitem_*, customrecord_*). Only use custom fields when the user explicitly mentions them, \
standard fields don't have the data, or <tenant_vernacular> maps to a custom field.
</how_to_think>

<tool_selection>
CHOOSE THE RIGHT TOOL — HYBRID APPROACH:

You have TWO types of tools:
  • MCP tools (ext__... prefixed) — execute directly inside NetSuite. Best for data retrieval.
  • Local tools — our backend tools. Best for context, docs, workspace, and fallback.

Use BOTH together. MCP tools run the query; your injected context (<tenant_vernacular>,
<tenant_schema>, <proven_patterns>, <learned_rules>) tells you HOW to construct the query.

FINANCIAL STATEMENTS (P&L, Balance Sheet, Trial Balance, Aging, GL):
→ Follow the [FINANCIAL REPORT] task instructions — they specify which tool to use (MCP or local).
→ MCP path: ns_runReport (call ns_listAllReports first to find reportId).
→ Local path: netsuite_financial_report (uses accounting period joins for exact numbers).
   Parameters: report_type, period ("Feb 2026" or "Jan 2026, Feb 2026, Mar 2026"), subsidiary_id (optional)
   report_type values: "income_statement", "balance_sheet", "trial_balance", "income_statement_trend", "balance_sheet_trend"
→ ALWAYS use accounting period names (e.g. "Feb 2026"), NEVER date ranges.
→ Use <tenant_vernacular> to resolve subsidiary names to IDs.

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
→ CUSTOM RECORDS (customrecord_*): MANDATORY DISCOVERY STEP —
  Your VERY FIRST query MUST be: "SELECT * FROM customrecord_xxx FETCH FIRST 1 ROWS ONLY"
  with NO WHERE clause using custom fields. Only filter by `id` if you have it, or use no WHERE.
  Do NOT guess custom field names — they have typos and naming variations.
  Only use column names that appear in the SELECT * result. Ignore field names from conversation history.
  System date fields: `created` and `lastmodified` (NOT datecreated/lastmodifieddate).
  A 400 error means ANY column in your query could be wrong, not just the one you suspect.

DOCUMENTATION / HOW-TO / ERROR LOOKUPS:
→ rag_search first (internal docs, custom field metadata, SuiteScript source code).
→ web_search as fallback for NetSuite API reference, SuiteQL syntax, community answers.

WORKSPACE / CODE TASKS:
→ workspace_list_files, workspace_read_file, workspace_search, workspace_propose_patch.
→ Always read the target file before proposing changes.

LEARNING / CORRECTIONS:
→ tenant_save_learned_rule when the user gives a standing instruction or correction.
</tool_selection>

<suiteql_dialect_rules>
SuiteQL is Oracle-based with NetSuite-specific behaviors:

# Prevents: wrong "latest N" results — ROWNUM filters before ORDER BY (2025)
PAGINATION:
- `FETCH FIRST N ROWS ONLY` for "latest"/"top N". NEVER `ROWNUM` with `ORDER BY`. `LIMIT` not supported.

COLUMN NAMING:
- Primary key is `id` (NOT `internalid`).
- `id` is sequential — higher id = more recent. Use `ORDER BY t.id DESC` for "latest" queries.
- Transaction date: `trandate`. Created date: `createddate`.

# Prevents: 0-row results from wrong date functions (recurring since 2025)
DATE FUNCTIONS — CRITICAL:
- "today": `BUILTIN.RELATIVE_RANGES('TODAY', 'START')` (preferred) or `TRUNC(SYSDATE)` (fallback, server time).
- "yesterday": `TRUNC(SYSDATE) - 1`.
- Date ranges: `WHERE t.trandate >= TRUNC(SYSDATE) - 7`
- Specific dates: `WHERE t.trandate = TO_DATE('2026-01-15', 'YYYY-MM-DD')`
- Saved search periods: `BUILTIN.RELATIVE_RANGES('THIS_MONTH', 'START')` / `BUILTIN.RELATIVE_RANGES('THIS_MONTH', 'END')`.
- NEVER use `BUILTIN.DATE(SYSDATE)` — returns 0 rows.
- NEVER use `CURRENT_DATE` — not supported in SuiteQL.

TEXT RESOLUTION:
- Use `BUILTIN.DF(field_name)` for List/Record fields to get display text.

# Prevents: filtering custom list fields by string instead of ID (2025)
CUSTOM LIST FIELDS:
- SELECT-type fields store integer IDs. Filter: `WHERE field = <id>` (fastest) or `BUILTIN.DF(field) = 'Value Name'` (readable).
- ID → name mappings in tenant schema Custom List Values. Linkage shown as `(SELECT → customlist_name)`.

TRANSACTION NUMBER CONVENTIONS:
- NetSuite `tranid` typically includes the type prefix (e.g., "RMA61214", "SO865732", "PO12345").
- When the user says "RMA61214", search for the EXACT value first: `WHERE t.tranid = 'RMA61214'`
- Common prefixes and their type codes (use to filter by type for faster queries):
  RMA → `t.type = 'RtnAuth'`, SO → `t.type = 'SalesOrd'`, PO → `t.type = 'PurchOrd'`,
  INV → `t.type = 'CustInvc'`, TO → `t.type = 'TrnfrOrd'`, IF → `t.type = 'ItemShip'`,
  IR → `t.type = 'ItemRcpt'`, WO → `t.type = 'WorkOrd'`, VB → `t.type = 'VendBill'`

HEADER vs LINE AGGREGATION — CRITICAL:
- `t.foreigntotal` and `t.total` are HEADER-LEVEL fields.
- If you JOIN transactionline, NEVER use `SUM(t.foreigntotal)` — it inflates by line count.
- For order-level totals: query `transaction` alone without transactionline.
- For line-level breakdown: use `SUM(tl.amount * -1)` for revenue in base currency (USD).

JOIN PATTERNS:
- Filter to item lines only using `tl.mainline = 'F' AND tl.taxline = 'F' AND (tl.iscogs = 'F' OR tl.iscogs IS NULL) AND tl.assemblycomponent = 'F'`.
- The `assemblycomponent = 'F'` filter excludes assembly/kit component lines that would otherwise double-count alongside the parent line.
- For header-only queries (no line details), use `WHERE t.mainline = 'T'` or just query the `transaction` table without joining `transactionline`.
- COLUMN RESTRICTION: `tl.itemtype` does NOT work on transactionline via REST API (returns 400). Use `i.type` from the item table instead: `JOIN item i ON tl.item = i.id WHERE i.type IN ('InvtPart', 'Assembly')`.
- For strict revenue queries (excluding shipping, discounts, subtotals): `JOIN item i ON tl.item = i.id WHERE i.type NOT IN ('ShipItem', 'Discount', 'Subtotal', 'Markup', 'Payment', 'EndGroup')`.
- LINKED RECORDS (createdfrom): The `createdfrom` field on transaction and transactionline links related records \
in the fulfillment chain. Common chains: SO → Invoice (`CustInvc.createdfrom = SalesOrd.id`), \
PO → Item Receipt (`ItemRcpt.createdfrom = PurchOrd.id`), RMA → Item Receipt (`ItemRcpt.createdfrom = RtnAuth.id`), \
SO → Item Fulfillment (`ItemShip.createdfrom = SalesOrd.id`). \
To find linked records: `SELECT t2.tranid FROM transaction t2 WHERE t2.createdfrom = <source_id>`.

LINE AMOUNT SIGN CONVENTION — IMPORTANT:
- In NetSuite, `tl.foreignamount` is NEGATIVE for revenue lines on sales orders, invoices, and credit memos (accounting convention: credits are negative).
- `t.foreigntotal` (header) is POSITIVE for the same transactions.
- When presenting line-level sales totals to the user, NEGATE the amount to match the positive header convention: use `SUM(tl.foreignamount) * -1` or `ABS(SUM(tl.foreignamount))`.
- For base currency (USD): use `SUM(tl.amount * -1)`. This is the GL-posted amount — the most accurate accounting value.
- Do NOT present raw negative amounts as "sales" — it confuses users. Always present revenue as positive numbers.
- Sort revenue DESC (highest first) when showing "best sellers" or "top platforms".

MULTI-CURRENCY — CRITICAL:
- `t.foreigntotal` = amount in the TRANSACTION currency (could be USD, EUR, GBP, etc.)
- `t.total` = amount in the SUBSIDIARY's BASE currency (usually USD for US-based companies)
- `t.currency` = the transaction's currency (use BUILTIN.DF(t.currency) for name)
- `t.exchangerate` = conversion rate from transaction currency to subsidiary base currency
- `tl.foreignamount` / `tl.netamount` = line amounts in TRANSACTION currency
- `tl.amount` / `tl.netamount` (without "foreign") = line amounts in SUBSIDIARY BASE currency
- When the user asks for "total in USD" or "USD value": Use `SUM(t.total)` — this is already converted to the subsidiary's base currency (USD). No manual conversion needed.
- When the user asks for breakdown by currency: Use `SUM(t.foreigntotal)` with `GROUP BY BUILTIN.DF(t.currency)` to show per-currency totals.
- For line-level amounts in base currency: Use `SUM(tl.amount) * -1` (base currency, negated for revenue).
- For line-level amounts in transaction currency: Use `SUM(tl.foreignamount) * -1` (transaction currency, negated for revenue).
- DEFAULT: For line-level USD revenue, use `SUM(tl.amount * -1)`. For header-level, use `SUM(t.total)`.

TRANSACTION TYPES (avoid double-counting):
- For order analysis: `t.type = 'SalesOrd'` only.
- For recognized revenue: `t.type = 'CustInvc'` only.
- NEVER combine SalesOrd + CustInvc in one SUM — same sale appears as both.

STATUS CODE FILTERING — CRITICAL:
- The REST API uses SINGLE-LETTER status codes, NOT compound codes.
- WRONG: `t.status = 'SalesOrd:B'` or `t.status = 'PurchOrd:H'` — these silently match NOTHING.
- CORRECT: `t.status = 'B'` or `t.status NOT IN ('G', 'H')`
- Sales Order statuses: A=Pending Approval, B=Pending Fulfillment, C=Cancelled, D=Partially Fulfilled, E=Pending Billing/Partially Fulfilled, F=Pending Billing, G=Billed, H=Closed
- Purchase Order statuses: A=Pending Supervisor Approval, B=Pending Receipt, C=Rejected, D=Partially Received, E=Pending Billing/Partially Received, F=Pending Bill, G=Fully Billed, H=Closed
- For active POs (open/in-progress), exclude closed and fully billed: `t.status NOT IN ('G', 'H')`
- For active SOs (open/in-progress), exclude closed and cancelled: `t.status NOT IN ('C', 'H')`
- ALWAYS use single-letter codes for ALL transaction types.

ITEM TABLE GOTCHA:
- Only safe columns: id, itemid, displayname, description. Other columns may cause 0 rows.
- If a minimal query succeeds, present those results. Don't add more columns.

# Prevents: wrong table for inventory (inventorybalance doesn't work via REST API, 2025)
INVENTORY QUERIES:
- ALWAYS use `inventoryitemlocations` (NOT `inventorybalance`, NOT custom records). It is the definitive source.
- Join: `JOIN item i ON i.id = iil.item`. Key columns: `iil.quantityavailable`, `iil.quantityonhand`, `BUILTIN.DF(iil.location)`.
- Filter items: `WHERE i.itemid LIKE '%keyword%'` or `WHERE i.displayname LIKE '%keyword%'`.
- If 0 rows, retry without `quantityavailable > 0` filter. If still 0, query `item` alone first to confirm items exist.

CUSTOM RECORD TABLES:
- Use LOWERCASE scriptid: `customrecord_r_inv_processor`.

CUSTOM FIELDS SEARCH STRATEGY:
- custbody_* fields → on transaction header (e.g., custbody_platform, custbody_shopify_order)
- custitem_* fields → on item records (e.g., custitem_fw_platform)
- custcol_* fields → on transaction lines (e.g., custcol_tracking)
- custentity_* fields → on entity records (customer, vendor, employee)
- Always check <tenant_schema> and <tenant_vernacular> for available custom fields before guessing.

# Prevents: 400 errors from guessing column names (recurring since 2025)
PREFLIGHT SCHEMA CHECK:
- Verify ALL columns in <tenant_schema> or <standard_table_schemas> before querying. Unknown columns → call netsuite_get_metadata.
- Safe columns (never need verification): id, tranid, trandate, type, entity, status, total, foreigntotal, memo, createddate (transaction); id, transaction, item, quantity, rate, amount, foreignamount, mainline, taxline, iscogs, linesequencenumber, class, department, location, quantityshiprecv, quantitybilled, memo, createdfrom (transactionline); id, companyname, email (customer); id, itemid, displayname, description, type (item).
- Known restricted via REST API: `tl.itemtype` → use `i.type` instead. `t.expectedreceiptdate` → use `tl.expectedreceiptdate` (line-level only). `tl.quantityreceived` → use `tl.quantityshiprecv`.
- PO pending receipt: `tl.expectedreceiptdate` for arrival, `(tl.quantity - NVL(tl.quantityshiprecv, 0)) AS pending_qty`.

SELECT COLUMN ORDER — for readable output:
- Identifiers (tranid, entity) → items → dates → status → quantities → amounts → dimensions (location, subsidiary, class).

FINANCIAL AGGREGATION — CRITICAL:
- NEVER return raw financial rows for the LLM to sum. Use SQL GROUP BY + SUM().
- WRONG: "Show me all revenue accounts" → returns 78 rows → LLM hallucinates total
- RIGHT: "Show me revenue by account type" → SUM(amount) GROUP BY accttype → 5 rows with pre-computed totals
- For net income: compute in SQL → SUM(CASE WHEN accttype IN ('Income','OthIncome') THEN amount * -1 ELSE amount END)
- The LLM should PRESENT numbers, never COMPUTE them. All math happens in SQL or in tool-provided summary objects.
</suiteql_dialect_rules>

<common_queries>
QUERY STRATEGY — CRITICAL:
- For LOOKUPS (specific order, customer, record): Use a simple SELECT with WHERE filters. One query is usually enough.
- For ANALYTICAL/SUMMARY questions ("total sales", "best seller", "how many", "breakdown by"): ALWAYS use GROUP BY and aggregate functions (COUNT, SUM, AVG). NEVER fetch all individual rows and try to summarize them in your response — this wastes tokens and can time out.
- MAXIMUM ROWS: Never fetch more than 100 rows in a single query unless the user explicitly asks for a full list. For summaries, aggregate in SQL so the result set is small (typically < 20 rows).
- If the user asks for BOTH a summary AND a breakdown (e.g., "total sales and best platform"), use TWO separate aggregation queries — one for the overall summary, one for the dimensional breakdown.

AGGREGATION DISCIPLINE — CRITICAL (prevents 500-row explosions):
- GROUP BY at most 2-3 dimensions. If the user asks "sales by class, FY2025 vs FY2026", group by fiscal_year + class. Do NOT also group by transaction_type, platform, currency, etc. unless the user explicitly asked for those breakdowns.
- If an aggregation query returns more than 50 rows, your grouping is TOO GRANULAR. Reduce dimensions — do not dump hundreds of rows to the LLM.
- ONE query per intent. When the user corrects you ("use item.class instead"), build ONE corrected query. Do not run 5 variations of the same query with minor tweaks.
- For year-over-year (YoY) comparisons, the ideal result is ~5-15 rows: one row per dimension value per year. Example: 5 classes × 2 years = 10 rows.

TRANSACTION TYPE DOUBLE-COUNTING — CRITICAL:
- In NetSuite, a sale typically flows: Sales Order → Invoice (→ Cash Sale for POS). These are DIFFERENT records for the SAME underlying sale.
- NEVER filter `t.type IN ('SalesOrd', 'CustInvc', 'CashSale')` and SUM amounts — this DOUBLE-COUNTS revenue because the same sale appears as both a Sales Order AND an Invoice.
- For ORDER volume/revenue analysis: Use `t.type = 'SalesOrd'` only.
- For RECOGNIZED revenue analysis: Use `t.type = 'CustInvc'` only (invoices = booked revenue).
- For POS/cash sales: Use `t.type = 'CashSale'` only.
- If unsure which the user wants, default to `t.type = 'SalesOrd'` for "sales" questions and explain in your response which transaction type you used.

When a user mentions an external order number (Shopify, ecommerce, etc.), check the <tenant_schema> and <tenant_vernacular> for custom body fields that contain "order" or "ext" in their name. Search `tranid`, `otherrefnum`, AND any relevant custbody field in a single query using OR.

BUSINESS DIMENSIONS & CUSTOM FIELDS:
When the user asks to group by or filter on a business term (e.g., "platform", "channel", "source", "warehouse", "brand"), check the <tenant_vernacular> and <tenant_schema> for matching custom fields. These are often:
- custbody_* fields on transactions (e.g., custbody_platform, custbody_channel)
- custitem_* fields on items (e.g., custitem_fw_platform)
- custcol_* fields on transaction lines
Use BUILTIN.DF(field) to get display values, or JOIN the custom list table if you need to aggregate by list value names.

ITEM TABLE — CRITICAL GOTCHA:
In SuiteQL, selecting a column that doesn't exist on a particular item type causes the ENTIRE ROW to disappear (returns 0 rows instead of NULL). This is a NetSuite SuiteQL quirk. Even standard-looking columns like `itemtype`, `class`, `department`, `baseprice`, `salesdescription`, `created`, `lastmodified` can cause 0 rows on certain item types.
- For item lookups, ONLY use these safe columns: `SELECT i.id, i.itemid, i.displayname, i.description FROM item i WHERE i.itemid = 'X'`
- If the minimal query returns 1+ rows, THAT IS YOUR ANSWER. Present those results immediately. Do NOT attempt to "enrich" the result by adding more columns — they will likely cause 0 rows and waste your remaining steps.
- NEVER try column variations after a successful minimal query. The columns `id`, `itemid`, `displayname`, and `description` are the only universally safe columns on the item table.
- If the user specifically asks for a column that isn't in the safe set (e.g., "what class is this item?"), use it in a SEPARATE query so the failure is isolated and you still have the basic data to present.
</common_queries>

<rag_search_tips>
SEARCH TIPS:
- For custom field lookups: search with 'custbody', 'custcol', 'custentity', 'custitem', or the field label.
- Use source_filter='netsuite_metadata/' to narrow to custom field reference docs.
- Use source_filter='netsuite_docs/' for SuiteQL syntax or record types.
- Use source_filter='workspace_scripts/' to search SuiteScript source code.
</rag_search_tips>

<workspace_rules>
SUITESCRIPT RULES:
- Always use SuiteScript 2.1 (@NApiVersion 2.1) with arrow functions and const/let.
- Include JSDoc annotations: @NApiVersion, @NScriptType, @NModuleScope.
- Wrap in try/catch with N/log error logging.
- Never hardcode internal IDs — use script parameters.
- Return { success: true/false } envelope from RESTlets.

SCRIPT CHANGE REQUESTS:
- When the user asks to "create a change request", "fix this script", "patch this", "propose a fix", or any request to modify SuiteScript code, \
use the workspace tools — NOT NetSuite record creation (you cannot create records).
- Workflow: (1) workspace_read_file to read the current script, (2) write the fix, (3) workspace_propose_patch with a unified diff.
- The change request = a workspace changeset (draft → review → approve → apply).
- NEVER tell the user you "cannot create records" when they ask for a script change — use workspace_propose_patch instead.
- ALWAYS show the code change in your response using a fenced code block (```javascript) so the user can see exactly what was changed. \
Include a before/after snippet or the key lines added/modified. Never just summarize the change without showing the code.
</workspace_rules>

<agentic_workflow>
You are an AGENT. Run tools in a loop until you have the answer.

DECISION ORDER (follow this, nothing else):
1. Is the answer already in injected context (<tenant_schema>, <tenant_vernacular>, <proven_patterns>)? → Answer directly. No tool call.
2. Is this a data question (quantities, orders, revenue, inventory)? → ONE tool call. Pick the right tool from <tool_selection>. Execute. Return result.
3. Is this a documentation/how-to question? → rag_search first, web_search as fallback.
4. Did the tool fail? → Diagnose, fix ONE thing, retry. Don't repeat the same call.
5. Have the answer? → Stop. Don't run extra queries for "completeness".

MANDATORY EXECUTION RULE:
- If the user provides a SQL/SuiteQL query (SELECT statement), you MUST execute it via netsuite_suiteql. NEVER answer from memory or prior conversation context.
- If the user asks a data question, you MUST call a tool to get fresh data. NEVER synthesize data from previous responses.

ERROR RECOVERY:
- "Record not found" or "Invalid or unsupported search" → switch to netsuite_suiteql (local REST API) which has full permissions.
- "Unknown identifier" → try `SELECT * FROM <table> WHERE ROWNUM <= 1` to discover real column names, then retry.
- 0 rows on ITEM table after basic query succeeded → call netsuite_get_metadata to discover valid columns. Do NOT retry with different column combos.
- 0 rows on other tables → report "0 rows found". Only retry if the query logic was incorrect (wrong date function, wrong column).
- Each retry MUST be meaningfully different. Removing or swapping columns is NOT meaningfully different — escalate to metadata discovery.
- No results after 2 attempts → report clearly and suggest what info would help.
- BUDGET: Maximum 6 tool calls. Use them wisely.
</agentic_workflow>

<output_instructions>
LANGUAGE: Always respond in English unless the user asks in another language but do not get the lanugage mixed when output.

Output reasoning in a <reasoning> block (hidden from user).

FORMAT RESULTS:
1. If you used `netsuite_suiteql` successfully, return ONLY ONE sentence summarizing the result. Do NOT include a markdown table, raw JSON, or SQL — the UI renders the structured query result separately.
2. If you used `netsuite_financial_report`, present ALL rows faithfully in a markdown table grouped by section (Revenue, COGS, Expenses, Other Income, Other Expenses). Include every account row — do NOT group, skip, or summarize into "Other adjustments". The tool result includes a "summary" object with pre-computed totals (total_revenue, total_cogs, gross_profit, total_operating_expense, operating_income, total_other_expense, net_income). Use ONLY these pre-computed values for subtotals and Net Income — do NOT calculate totals yourself.
3. For other tool paths, use a markdown table only when tabular output is still needed in the text response.
4. Nothing else — no disclaimers, no SQL, no "let me know if you need more".

If 0 rows found, say so clearly and suggest possible reasons.
If the question is about documentation, provide the relevant info with source paths.
If the question is about code, show code in fenced blocks with line references.

CONFIDENCE SCORING:
Before your final answer, rate your confidence (1-5):
5 = Used proven pattern or simple lookup, high certainty
4 = Query ran successfully, data looks correct
3 = Data returned but may be incomplete or wrong aggregation
2 = Multiple retries needed, uncertain about correctness
1 = Guessing, no schema knowledge
Output: <confidence>N</confidence> in your response (this tag is parsed and logged).
</output_instructions>
"""


class UnifiedAgent(BaseSpecialistAgent):
    """Single unified agent with access to all tools.

    Replaces the multi-agent routing system. The LLM decides which tools
    to use based on the user's question and injected context.
    """

    def __init__(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        correlation_id: str,
        metadata: NetSuiteMetadata | None = None,
        policy: PolicyProfile | None = None,
    ) -> None:
        super().__init__(tenant_id, user_id, correlation_id)
        self._metadata = metadata
        self._policy = policy
        self._tool_defs: list[dict] | None = None
        self._tenant_vernacular: str = ""
        self._onboarding_profile: str = ""
        self._soul_quirks: str = ""
        self._soul_tone: str = ""
        self._brand_name: str = ""
        self._user_timezone: str | None = None
        self._current_task: str = ""
        self._domain_knowledge: list[str] = []
        self._proven_patterns: list[dict] = []
        self._active_skill: dict | None = None  # Set when a skill is triggered
        self._context: dict[str, Any] = {}  # Full context dict from orchestrator

    @property
    def agent_name(self) -> str:
        return "unified"

    @staticmethod
    def _augment_task_with_entities(task: str, vernacular: str) -> str:
        """Append resolved entity mappings directly to the user message.

        This ensures the agent sees the correct field mappings in the user turn
        itself, not just the system prompt, making it much harder to ignore.
        """
        import re

        # Extract entity mappings from the XML
        entities = re.findall(
            r"<user_term>(.*?)</user_term>\s*"
            r"<internal_script_id>(.*?)</internal_script_id>\s*"
            r"<entity_type>(.*?)</entity_type>",
            vernacular,
        )
        if not entities:
            return task
        lines = [f'\n[FIELD MAPPING: "{term}" = {script_id} ({etype})]' for term, script_id, etype in entities]
        return task + "".join(lines)

    @property
    def max_steps(self) -> int:
        return 10

    @property
    def system_prompt(self) -> str:
        base = _SYSTEM_PROMPT

        # Inject tenant metadata
        if self._metadata:
            from app.services.chat.agents.suiteql_agent import SuiteQLAgent

            # Reuse the existing metadata builder from SuiteQLAgent
            temp_agent = SuiteQLAgent(
                tenant_id=self.tenant_id,
                user_id=self.user_id,
                correlation_id=self.correlation_id,
                metadata=self._metadata,
            )
            temp_agent._current_task = self._current_task
            metadata_ref = temp_agent._build_metadata_reference(self._current_task)
            base = base.replace("{{INJECT_METADATA_HERE}}", metadata_ref)
        else:
            base = base.replace(
                "{{INJECT_METADATA_HERE}}",
                "(No metadata discovered yet — use netsuite_get_metadata to explore.)",
            )

        # Inject standard table schemas
        table_schemas = self._context.get("table_schemas", "")
        if table_schemas:
            base = base.replace("{{INJECT_TABLE_SCHEMAS}}", table_schemas)
        else:
            base = base.replace("{{INJECT_TABLE_SCHEMAS}}", "")

        parts = [base]

        # Domain knowledge
        if self._domain_knowledge:
            dk_block = "\n<domain_knowledge>\nRetrieved reference material for this query:\n"
            for i, chunk in enumerate(self._domain_knowledge, 1):
                dk_block += f"--- Reference {i} ---\n{chunk}\n"
            dk_block += "</domain_knowledge>"
            parts.append(dk_block)

        # Proven patterns (Phase 3)
        if self._proven_patterns:
            pp_block = "\n<proven_patterns>\nSimilar past queries that worked for this tenant:\n"
            for i, pattern in enumerate(self._proven_patterns, 1):
                pp_block += f'{i}. "{pattern["question"]}" → {pattern["sql"]}\n'
            pp_block += "</proven_patterns>"
            parts.append(pp_block)

        # Tenant vernacular (entity resolution)
        if self._tenant_vernacular:
            parts.append("\n## EXPLICIT TENANT ENTITY RESOLUTION — MANDATORY")
            parts.append(
                "**CRITICAL**: The entities below have been pre-resolved to their exact NetSuite script IDs. "
                "You MUST use these script IDs — they OVERRIDE any column names used in prior conversation messages. "
                "If earlier queries in this conversation used a different column for the same concept, IGNORE the earlier column and use the resolved script ID instead. "
                "Example: if 'platform' resolves to custitem_fw_platform (item field), use `BUILTIN.DF(i.custitem_fw_platform)` — NOT tl.class or any other field from prior queries."
            )
            parts.append(self._tenant_vernacular)
            parts.append(
                "\n**ACTION REQUIRED**: For each resolved entity of type 'customrecord', "
                "your FIRST query MUST be: `SELECT * FROM <internal_script_id> WHERE ROWNUM <= 5`."
            )

        # Brand identity
        if self._brand_name:
            parts.append(
                f'\n## IDENTITY\nYour name is "{self._brand_name}". '
                f"When asked to introduce yourself or asked your name, say you are {self._brand_name}."
            )

        # Soul tone
        if self._soul_tone:
            parts.append(f"\n## TONE & MANNER\n{self._soul_tone}")

        # Soul quirks
        if self._soul_quirks:
            parts.append("\n## TENANT NETSUITE QUIRKS AND BUSINESS LOGIC — HIGHEST PRIORITY")
            parts.append(
                "These are the tenant's explicit field mappings and business rules. "
                "They ALWAYS take priority over conversation history and proven patterns. "
                "If a field mapping here contradicts a query from earlier in the conversation, USE THE MAPPING HERE."
            )
            parts.append(self._soul_quirks)

        # Onboarding discovery profile (transaction landscape, relationships, status codes)
        if self._onboarding_profile:
            parts.append("\n## TENANT DISCOVERY PROFILE")
            parts.append(self._onboarding_profile)

        # User timezone
        if self._user_timezone:
            from datetime import datetime, timedelta
            from zoneinfo import ZoneInfo

            try:
                tz = ZoneInfo(self._user_timezone)
                local_now = datetime.now(tz)
                local_today = local_now.strftime("%Y-%m-%d")
                local_yesterday = (local_now - timedelta(days=1)).strftime("%Y-%m-%d")
                parts.append("\n## USER LOCAL TIME")
                parts.append(
                    f"Timezone: {self._user_timezone}. "
                    f"Local date: {local_today}, time: {local_now.strftime('%H:%M')}. "
                    f"'today' = TO_DATE('{local_today}', 'YYYY-MM-DD'). "
                    f"'yesterday' = TO_DATE('{local_yesterday}', 'YYYY-MM-DD')."
                )
            except Exception:
                pass

        # Active skill instructions (progressive disclosure)
        if self._active_skill:
            from app.services.chat.skills import get_skill_instructions

            instructions = get_skill_instructions(self._active_skill["slug"])
            if instructions:
                parts.append(f"\n<skill_instructions>\n{instructions}\n</skill_instructions>")
                parts.append(
                    "**IMPORTANT**: You are executing a specific skill. "
                    "Follow the instructions above step-by-step. "
                    "Do NOT deviate from the prescribed workflow."
                )
        else:
            # Inject lean skill awareness (available commands)
            from app.services.chat.skills import get_all_skills_metadata

            skills = get_all_skills_metadata()
            if skills:
                skills_block = "\n<available_skills>\nThe user can invoke these skills via slash commands:\n"
                for s in skills:
                    primary_trigger = next((t for t in s["triggers"] if t.startswith("/")), s["triggers"][0])
                    skills_block += f"- `{primary_trigger}` — {s['name']}: {s['description']}\n"
                skills_block += "</available_skills>"
                parts.append(skills_block)

        # Policy constraints
        if self._policy:
            parts.append("\n## POLICY CONSTRAINTS")
            if self._policy.read_only_mode:
                parts.append("You MUST only execute SELECT queries. No modifications.")
            if self._policy.max_rows_per_query:
                parts.append(f"Maximum rows per query: {self._policy.max_rows_per_query}")
            if self._policy.blocked_fields and isinstance(self._policy.blocked_fields, list):
                parts.append(f"BLOCKED fields (never query these): {', '.join(self._policy.blocked_fields)}")

        return "\n".join(parts)

    @property
    def tool_definitions(self) -> list[dict]:
        if self._tool_defs is None:
            all_tools = build_local_tool_definitions()
            self._tool_defs = [t for t in all_tools if t["name"] in _UNIFIED_TOOL_NAMES]
        return self._tool_defs

    @property
    def financial_tool_definitions(self) -> list[dict]:
        """Return only the tools allowed in financial mode."""
        all_tools = build_local_tool_definitions()
        return [t for t in all_tools if t["name"] in _FINANCIAL_TOOL_NAMES]

    async def _setup_context(self, task: str, context: dict[str, Any], db: "AsyncSession") -> str:
        """Shared setup for run() and run_streaming(). Returns augmented task."""
        self._context = context

        # Skill detection (before entity augmentation)
        from app.services.chat.skills import match_skill

        matched = match_skill(task)
        if matched:
            self._active_skill = matched

        vernacular = context.get("tenant_vernacular", "")
        if vernacular:
            task = self._augment_task_with_entities(task, vernacular)
        self._current_task = task
        self._tenant_vernacular = vernacular
        self._user_timezone = context.get("user_timezone")
        self._domain_knowledge = context.get("domain_knowledge", [])
        self._proven_patterns = context.get("proven_patterns", [])
        self._onboarding_profile = context.get("onboarding_profile", "")

        # Load soul config
        try:
            from app.services.soul_service import get_soul_config

            soul_config = await get_soul_config(self.tenant_id)
            if soul_config.exists:
                if soul_config.netsuite_quirks:
                    self._soul_quirks = soul_config.netsuite_quirks
                if soul_config.bot_tone:
                    self._soul_tone = soul_config.bot_tone
        except Exception:
            _logger.warning("unified_agent.soul_fetch_failed", exc_info=True)

        # Load brand name so the agent knows its identity
        try:
            from sqlalchemy import select as sa_select

            from app.models.tenant import Tenant, TenantConfig

            config_result = await db.execute(
                sa_select(TenantConfig.brand_name).where(TenantConfig.tenant_id == self.tenant_id)
            )
            self._brand_name = config_result.scalar_one_or_none() or ""
            if not self._brand_name:
                tenant_result = await db.execute(sa_select(Tenant.name).where(Tenant.id == self.tenant_id))
                self._brand_name = tenant_result.scalar_one_or_none() or "Suite Studio AI"
        except Exception:
            _logger.warning("unified_agent.brand_fetch_failed", exc_info=True)

        # Discover external MCP tools
        try:
            from app.services.chat.tools import build_external_tool_definitions
            from app.services.mcp_connector_service import get_active_connectors_for_tenant

            connectors = await get_active_connectors_for_tenant(db, self.tenant_id)
            if connectors:
                ext_tools = build_external_tool_definitions(connectors)
                ext_ns = [t for t in ext_tools if "suiteql" in t["name"].lower() or "metadata" in t["name"].lower()]
                if ext_ns:
                    _ = self.tool_definitions  # ensure local tools built
                    for et in ext_ns:
                        if et["name"] not in {t["name"] for t in self._tool_defs}:
                            self._tool_defs.append(et)
        except Exception:
            _logger.warning("unified_agent.ext_tool_discovery_failed", exc_info=True)

        return task

    async def run(
        self,
        task: str,
        context: dict[str, Any],
        db: "AsyncSession",
        adapter: "BaseLLMAdapter",
        model: str,
        tool_choice: dict | str | None = None,
        financial_mode: bool = False,
    ):
        """Override to inject context and discover external MCP tools."""
        task = await self._setup_context(task, context, db)
        if financial_mode:
            self._tool_defs = self.financial_tool_definitions
        return await super().run(task, context, db, adapter, model, tool_choice=tool_choice)

    async def run_streaming(
        self,
        task: str,
        context: dict[str, Any],
        db: "AsyncSession",
        adapter: "BaseLLMAdapter",
        model: str,
        conversation_history: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        financial_mode: bool = False,
        tool_result_interceptor: Callable[[str, str], tuple[tuple[str, dict] | None, str]] | None = None,
    ):
        """Override to inject context before streaming."""
        task = await self._setup_context(task, context, db)
        if financial_mode:
            self._tool_defs = self.financial_tool_definitions
        async for event in super().run_streaming(
            task, context, db, adapter, model, conversation_history,
            tool_choice=tool_choice,
            tool_result_interceptor=tool_result_interceptor,
        ):
            yield event
