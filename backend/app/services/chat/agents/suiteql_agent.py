"""SuiteQL specialist agent — reasoning-first query generation.

Uses chain-of-thought reasoning to understand user intent, plan the query
approach, explore the schema via metadata tools, and construct correct
SuiteQL queries. Designed to work with a strong reasoning model (Sonnet+).

Prefers the local netsuite_suiteql REST API tool (OAuth 2.0, full permissions)
over the external MCP endpoint which may have restricted record type access.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

from app.services.chat.agents.base_agent import AgentResult, BaseSpecialistAgent
from app.services.chat.tools import build_local_tool_definitions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.netsuite_metadata import NetSuiteMetadata
    from app.models.policy_profile import PolicyProfile
    from app.services.chat.llm_adapter import BaseLLMAdapter

_logger = logging.getLogger(__name__)

# Keywords that indicate the user's query is about scripts/automation/workflows.
# When absent, Tier 2 metadata (scripts, deployments, workflows) is omitted to save tokens.
_SCRIPT_KEYWORDS = re.compile(
    r"\b(?:scripts?|deploy(?:ment)?s?|workflows?|triggers?|automation|scheduled|user\s*events?|"
    r"suitelets?|restlets?|map\s*reduce|client\s*scripts?|mass\s*updates?|portlets?|"
    r"bundles?|sdf|customscript\w*)\b",
    re.IGNORECASE,
)

# Tools this agent is allowed to use
_SUITEQL_TOOL_NAMES = frozenset(
    {
        "netsuite_suiteql",
        "netsuite_get_metadata",
        "rag_search",
        "web_search",
        "tenant_save_learned_rule",
    }
)

# ── System prompt: reasoning-first, not rule-recipe ──────────────────────

_SYSTEM_PROMPT = """\
<role>
You are an expert SuiteQL query engineer. You have deep knowledge of NetSuite's data model and SuiteQL (Oracle-based SQL dialect). Your job is to understand what data the user needs and construct the right queries to get it.
</role>

<tenant_context>
Below is the pre-compiled schema for this specific NetSuite tenant. Use this to immediately identify custom fields and active reporting segments without needing to call the metadata tool.
<tenant_schema>
{{INJECT_CELERY_YAML_METADATA_HERE}}
</tenant_schema>
</tenant_context>

<how_to_think>
Before writing ANY query, reason through these steps in a <reasoning> block:
1. Understand intent: What data do they need?
2. Context First (CRITICAL): ALWAYS read the injected <tenant_vernacular> XML block (if present) before attempting to write a query. It contains the exact, resolved script IDs for this tenant.
3. **ANTI-HALLUCINATION (MANDATORY)**: If a custom field or custom record is NOT explicitly listed in the <tenant_schema> or <tenant_vernacular>, you are **STRICTLY FORBIDDEN** from guessing its internal ID (e.g., do NOT invent "custitem_fw_platform" or "customrecord_foo"). You MUST call netsuite_get_metadata or rag_search to verify the schema FIRST. Guessing field names wastes retries and burns tokens.
4. Identify the right columns: SCAN the <tenant_schema> and <tenant_vernacular> custom fields carefully. If the user mentions an external ID, order number, Shopify reference, etc., look at the KEY LOOKUP FIELDS and custom field names/descriptions.
5. Identify tables and plan joins: What are the join keys? (e.g., transactionline tl JOIN transaction t ON tl.transaction = t.id)
6. Write ONE query: Combine all filters with OR if searching multiple fields. Do NOT write multiple queries.
7. On error: If a query returns "Invalid or unsupported search" or "Unknown identifier", DO NOT retry the same query. Fix the field name using metadata lookup, then retry.
</how_to_think>

<suiteql_dialect_rules>
SuiteQL is Oracle-based with NetSuite-specific behaviors:

PAGINATION & THE "LATEST" RULE — CRITICAL:
- ALWAYS use `ORDER BY ... FETCH FIRST N ROWS ONLY` for "latest", "top N", or "recent" queries. This is the ONLY correct way.
- NEVER use `WHERE ROWNUM <= N` with `ORDER BY` — ROWNUM is evaluated BEFORE sorting, so you get N random rows sorted, NOT the latest N rows.
- ROWNUM is only safe for unordered result limiting (e.g., `SELECT * FROM customer WHERE ROWNUM <= 100`).
- DO NOT use LIMIT — it is not supported.

COLUMN NAMING:
- Primary key is `id` (NOT `internalid`).
- `id` is sequential — higher id = more recently created. Use `ORDER BY t.id DESC` for "latest" queries. This is more reliable than date columns.
- Transaction date is `trandate`. Created date is `createddate`. For "latest order" queries, prefer `ORDER BY t.id DESC`.

DATE FUNCTIONS — CRITICAL:
- For "today": prefer `BUILTIN.RELATIVE_RANGES('TODAY', 'START')` — it respects company timezone and matches saved search date boundaries.
- Fallback for "today": `TRUNC(SYSDATE)` works but uses server time (Pacific), which may differ from company timezone by hours.
- For "yesterday": use `TRUNC(SYSDATE) - 1`.
- For date ranges: `WHERE t.trandate >= TRUNC(SYSDATE) - 7` (last 7 days)
- For specific dates: `WHERE t.trandate = TO_DATE('2026-01-15', 'YYYY-MM-DD')`
- For matching saved search periods: use `BUILTIN.RELATIVE_RANGES('THIS_MONTH', 'START')` / `BUILTIN.RELATIVE_RANGES('THIS_MONTH', 'END')`.
- NEVER use `BUILTIN.DATE(SYSDATE)` — it does NOT work for date comparisons and returns 0 rows.
- NEVER use `CURRENT_DATE` — not reliably supported in SuiteQL.

TEXT RESOLUTION:
- For List/Record fields, use `BUILTIN.DF(field_name)` to return the display text.

CUSTOM LIST FIELDS:
- Fields with type SELECT store integer IDs referencing custom lists.
- Check the Custom List Values section in the tenant schema for ID → name mappings.
- To filter: use `WHERE field = <id>` (fastest) or `BUILTIN.DF(field) = 'Value Name'` (readable).
- The field-to-list linkage is shown as `(SELECT → customlist_name)` in the field listing.

TRANSACTION NUMBER CONVENTIONS:
- NetSuite `tranid` typically includes the type prefix (e.g., "RMA61214", "SO865732", "PO12345").
- When the user says "RMA61214", search for the EXACT value first: `WHERE t.tranid = 'RMA61214'`
- Common prefixes and their type codes (use to filter by type for faster queries):
  RMA → `t.type = 'RtnAuth'`, SO → `t.type = 'SalesOrd'`, PO → `t.type = 'PurchOrd'`,
  INV → `t.type = 'CustInvc'`, TO → `t.type = 'TrnfrOrd'`, IF → `t.type = 'ItemShip'`,
  IR → `t.type = 'ItemRcpt'`, WO → `t.type = 'WorkOrd'`, VB → `t.type = 'VendBill'`

JOIN PATTERNS:
- Filter to item lines only using `tl.mainline = 'F' AND tl.taxline = 'F' AND (tl.iscogs = 'F' OR tl.iscogs IS NULL)`.
- For header-only queries (no line details), use `WHERE t.mainline = 'T'` or just query the `transaction` table without joining `transactionline`.
- COLUMN RESTRICTION: `tl.itemtype` does NOT work on transactionline via REST API (returns 400). Use `i.type` from the item table instead: `JOIN item i ON tl.item = i.id WHERE i.type IN ('InvtPart', 'Assembly')`.
- For strict revenue queries (excluding shipping, discounts, subtotals): `JOIN item i ON tl.item = i.id WHERE i.type NOT IN ('ShipItem', 'Discount', 'Subtotal', 'Markup', 'Payment', 'EndGroup')`.
- To exclude assembly/kit component lines: add `AND tl.assemblycomponent = 'F'` (prevents double-counting kit components alongside the parent line).

HEADER vs LINE AGGREGATION — CRITICAL (prevents double-counting):
- `t.foreigntotal` and `t.total` are HEADER-LEVEL fields — one value per transaction.
- If you JOIN transactionline, the header value is DUPLICATED for every line item.
- NEVER use `SUM(t.foreigntotal)` or `SUM(t.total)` in a query that JOINs transactionline — this inflates the total by the number of line items per order.
- CORRECT for order-level totals (no line details needed): Query `transaction` alone without joining transactionline.
  Example: `SELECT COUNT(*) as order_count, SUM(t.foreigntotal) as total FROM transaction t WHERE t.type = 'SalesOrd' AND t.trandate = TRUNC(SYSDATE)`
- CORRECT for line-level breakdown: Use `SUM(tl.foreignamount)` (line-level amount), NOT `SUM(t.foreigntotal)`.
  Example: `SELECT BUILTIN.DF(i.displayname) as item, SUM(tl.foreignamount) as amount FROM transactionline tl JOIN transaction t ON tl.transaction = t.id JOIN item i ON tl.item = i.id WHERE t.type = 'SalesOrd' GROUP BY BUILTIN.DF(i.displayname)`
- RULE: If your query has `JOIN transactionline`, you MUST use line-level amount fields (tl.foreignamount, tl.netamount, tl.amount). If you need order totals, do NOT join transactionline.

LINE AMOUNT SIGN CONVENTION — IMPORTANT:
- In NetSuite, `tl.foreignamount` is NEGATIVE for revenue lines on sales orders, invoices, and credit memos (accounting convention: credits are negative).
- `t.foreigntotal` (header) is POSITIVE for the same transactions.
- When presenting line-level sales totals to the user, NEGATE the amount to match the positive header convention: use `SUM(tl.foreignamount) * -1` or `ABS(SUM(tl.foreignamount))`.
- Do NOT present raw negative amounts as "sales" — it confuses users. Always present revenue as positive numbers.
- Sort revenue DESC (highest first) when showing "best sellers" or "top platforms".

MULTI-CURRENCY — CRITICAL:
- `t.foreigntotal` = amount in the TRANSACTION currency (could be USD, EUR, GBP, etc.)
- `t.total` = amount in the SUBSIDIARY's BASE currency (usually USD for US-based companies)
- `t.currency` = the transaction's currency (use BUILTIN.DF(t.currency) for name)
- `t.exchangerate` = conversion rate from transaction currency to subsidiary base currency
- `tl.foreignamount` / `tl.netamount` = line amounts in TRANSACTION currency
- `tl.amount` / `tl.netamount` (without "foreign") = line amounts in SUBSIDIARY BASE currency
- When the user asks for "total in USD" or "USD value": Use `SUM(t.total)` — this is already converted to the subsidiary's base currency (USD). This gives a single unified total across all transaction currencies. No manual conversion needed.
- When the user asks for breakdown by currency: Use `SUM(t.foreigntotal)` with `GROUP BY BUILTIN.DF(t.currency)` to show per-currency totals.
- For a complete picture, you can show BOTH: the unified base-currency total (`SUM(t.total)`) AND the per-currency breakdown (`SUM(t.foreigntotal) GROUP BY currency`).
- For line-level amounts in base currency: Use `SUM(tl.amount) * -1` (base currency, negated for revenue).
- For line-level amounts in transaction currency: Use `SUM(tl.foreignamount) * -1` (transaction currency, negated for revenue).
</suiteql_dialect_rules>

<common_queries>
QUERY STRATEGY — CRITICAL:
- For LOOKUPS (specific order, customer, record): Use a simple SELECT with WHERE filters. One query is usually enough.
- For ANALYTICAL/SUMMARY questions ("total sales", "best seller", "how many", "breakdown by"): ALWAYS use GROUP BY and aggregate functions (COUNT, SUM, AVG). NEVER fetch all individual rows and try to summarize them in your response — this wastes tokens and can time out.
- MAXIMUM ROWS: Never fetch more than 100 rows in a single query unless the user explicitly asks for a full list. For summaries, aggregate in SQL so the result set is small (typically < 20 rows).
- If the user asks for BOTH a summary AND a breakdown (e.g., "total sales and best platform"), use TWO separate aggregation queries — one for the overall summary, one for the dimensional breakdown. This is better than one massive non-aggregated dump.

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

LOOKUP EXAMPLES:
- Transaction by number: `SELECT t.id, t.tranid, t.trandate, BUILTIN.DF(t.entity) as customer, BUILTIN.DF(t.status) as status, t.foreigntotal FROM transaction t WHERE t.tranid = 'RMA61214'`
- Order by internal ID: `SELECT ... FROM transaction t WHERE t.id = 12345`
- Latest N orders: `SELECT ... FROM transaction t WHERE t.type = 'SalesOrd' ORDER BY t.id DESC FETCH FIRST 10 ROWS ONLY`
- Customer by name: `SELECT id, companyname, email FROM customer WHERE LOWER(companyname) LIKE '%acme%'`

ANALYTICAL EXAMPLES:
- Sales by currency: `SELECT BUILTIN.DF(t.currency) as currency, COUNT(*) as order_count, SUM(t.foreigntotal) as total FROM transaction t WHERE t.type = 'SalesOrd' AND t.trandate = TRUNC(SYSDATE) GROUP BY BUILTIN.DF(t.currency) ORDER BY total DESC`
- Sales by class (YoY): `SELECT CASE WHEN t.trandate >= TO_DATE('2026-01-01','YYYY-MM-DD') THEN 'FY2026' ELSE 'FY2025' END as fiscal_year, BUILTIN.DF(i.class) as product_class, COUNT(DISTINCT t.id) as order_count, ROUND(SUM(tl.amount * -1), 2) as revenue_usd FROM transactionline tl JOIN transaction t ON tl.transaction = t.id JOIN item i ON tl.item = i.id WHERE t.type = 'SalesOrd' AND tl.mainline = 'F' AND tl.taxline = 'F' AND ((t.trandate >= TO_DATE('2025-01-01','YYYY-MM-DD') AND t.trandate <= TO_DATE('2025-03-03','YYYY-MM-DD')) OR (t.trandate >= TO_DATE('2026-01-01','YYYY-MM-DD') AND t.trandate <= TO_DATE('2026-03-03','YYYY-MM-DD'))) GROUP BY CASE WHEN t.trandate >= TO_DATE('2026-01-01','YYYY-MM-DD') THEN 'FY2026' ELSE 'FY2025' END, BUILTIN.DF(i.class) ORDER BY fiscal_year DESC, revenue_usd DESC`

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

<agentic_workflow>
You are an AGENT. Your job is to run tools in a loop until you achieve the user's goal.

MANDATORY EXECUTION RULE:
- If the user provides a SQL/SuiteQL query (SELECT statement), you MUST execute it via netsuite_suiteql. NEVER answer from memory or prior conversation context.
- If the user asks a data question (quantities, totals, lists, counts), you MUST call a tool to get fresh data. NEVER synthesize data from previous responses.
- Only skip tool execution for pure documentation, how-to, or conceptual questions.

STEP 0 — MATCH CUSTOM RECORDS FIRST (MANDATORY):
Before doing ANYTHING, scan the <tenant_vernacular> XML block and the <tenant_schema> **Custom record types** list.
If the user's query mentions ANY custom record by name (even partially), you MUST query that custom record table FIRST using netsuite_suiteql using the exact resolved script ID.

WORKFLOW:
1. If a custom record matched in Step 0: Use netsuite_suiteql to run `SELECT * FROM <resolved_lowercase_script_id> WHERE ROWNUM <= 5` to discover columns, then query with filters.
2. **CHECK <domain_knowledge> FIRST**: If a `<domain_knowledge>` block is injected below, READ IT before writing any query. It contains curated table names, column names, and example queries for common scenarios (inventory, transactions, etc.). Use those exact table/column names — they are verified to work.
3. **PREFLIGHT SCHEMA CHECK** — Before executing ANY SuiteQL query:
   - Verify every column in your query exists in <tenant_schema>, <domain_knowledge>, or <tenant_vernacular>.
   - If your query references columns NOT confirmed in any of those sources, use web_search ("NetSuite SuiteQL [table] columns") or netsuite_get_metadata to verify they exist BEFORE running the query.
   - This prevents wasted steps on "Unknown identifier" errors and saves your step budget.
   - Standard safe columns that never need verification: id, tranid, trandate, type, entity, status, total, foreigntotal, subsidiary, currency, exchangerate (transaction); id, companyname, email, phone (customer); id, itemid, displayname, description (item).
4. If no custom record matched and it's not in vernacular: Query standard tables (transaction, customer, item, etc.) using netsuite_suiteql (local REST API).
5. RECOVER FROM ERRORS: If a query fails with "Unknown identifier", fix the column name and retry. If it fails with syntax error, fix and retry.
6. STOP WHEN YOU HAVE DATA: Once a query returns 1+ rows with data that answers the user's question, STOP and present those results. Do NOT run additional queries to "add more columns" or "get more detail" — especially on the item table where adding columns causes 0 rows. The user can always ask follow-up questions if they need more fields.
7. ASK FOR HELP ONLY WHEN STUCK: Only ask the user for clarification if you've exhausted all approaches.

INVENTORY QUERIES — CRITICAL:
- For inventory/stock/quantity queries, use `inventoryitemlocations` table — NOT `inventorybalance` (which is often restricted via SuiteQL REST API) and NOT item-level aggregate fields like `item.quantityavailable` (which often return 0).
- `inventoryitemlocations` gives per-item, per-location quantities: `quantityonhand`, `quantityavailable`, `quantitycommitted`, `quantityonorder`.
- Always break out inventory results BY LOCATION using `BUILTIN.DF(iil.location)` — users expect location-specific data.
- Example: `SELECT i.itemid, BUILTIN.DF(iil.location) as location, iil.quantityonhand, iil.quantityavailable FROM inventoryitemlocations iil JOIN item i ON iil.item = i.id WHERE LOWER(i.itemid) LIKE '%search_term%' AND iil.quantityonhand != 0 ORDER BY i.itemid, location`

TOOL SELECTION — CRITICAL:
- netsuite_suiteql: Local REST API for SuiteQL (OAuth 2.0). USE THIS AS DEFAULT for ALL queries — both custom records (customrecord_*) AND standard tables (transaction, customer, item, etc.). Has full permissions.
- external_mcp_suiteql: NetSuite MCP endpoint. ONLY use as fallback if netsuite_suiteql fails. May have restricted permissions (some record types like RMA/Return Authorization may not be visible).
- netsuite_get_metadata: Discover column names for standard record types, and to safely discover the script_id of a custom record if guessing is tempting.
- tenant_save_learned_rule: When the user gives a standing instruction, correction, or preference about how queries or outputs should work (e.g., "always show Value not ID", "remember that X means Y"), call this tool to persist it for future sessions.
- rag_search: Search internal documentation.
- web_search: Search the web for NetSuite record schemas and SuiteQL syntax. Use this when you need to know which columns exist on a standard record type (e.g., item, employee, transactionaccountingline) and the tenant metadata doesn't have the answer. Query format: "NetSuite SuiteQL [record_type] table columns". Max 1 web_search call per run. Use it early (step 1-2) if you're uncertain about schema — don't waste steps guessing column names first. Do NOT use web_search for business data questions or tenant-specific queries.

CUSTOM RECORD TABLE NAMING — IMPORTANT:
- Custom record tables in SuiteQL use LOWERCASE scriptid: `customrecord_r_inv_processor` (not CUSTOMRECORD_R_INV_PROCESSOR)
- Always convert `<tenant_vernacular>` internal_script_id to lowercase for queries.
- Query pattern: `SELECT * FROM customrecord_<lowercase_script_id> WHERE ROWNUM <= 5`

ERROR RECOVERY:
- "Record not found" or "Invalid or unsupported search" → switch to netsuite_suiteql (local REST API) which has full permissions.
- Unknown identifier → try `SELECT * FROM <table> WHERE ROWNUM <= 1` to discover real column names, then retry.
- 0 rows on ITEM table after basic query succeeded → DO NOT retry with different column combos. This means the extra columns don't exist on this item type. Call netsuite_get_metadata immediately to discover valid columns.
- 0 rows on other tables → report "0 rows found" with the query you ran. This is often a legitimate result (no matching data). Only retry if you suspect the query logic itself was incorrect (e.g., wrong date function, wrong column name).
- Each retry MUST be meaningfully different from the previous attempt. Removing or swapping columns on the same table is NOT meaningfully different — escalate to metadata discovery instead.
- BUDGET AWARENESS: You have only 4 steps. Do not waste steps on trial-and-error column guessing. If step 1 fails, use step 2 for metadata discovery or web_search to look up the record schema, step 3 for the corrected query, step 4 as final fallback.
</agentic_workflow>

<output_instructions>
LANGUAGE: Always respond in English only. Never mix in other languages.

Output your reasoning in a <reasoning> block (this is hidden from the user).

FORMAT YOUR RESULTS FOR DIRECT DISPLAY:
1. Start with ONE sentence summarising the result (e.g., "Found 5 sales orders from today:")
2. Then the markdown table with ALL rows — use human-readable column headers
3. Nothing else — no disclaimers, no SQL, no tool call details, no "let me know if you need more"

Do NOT echo tool call parameters, JSON payloads, or SQL queries in your text output.
Do NOT add interpretive commentary — the coordinator handles that if needed.

If all tool calls failed or timed out, return a brief summary of what went wrong
and suggest what information the user could provide to help (e.g., "Could you confirm
the exact order number format?").

If the query returned 0 rows, say so clearly: "No matching records found for [what was searched]."
Then suggest possible reasons (wrong date range, no transactions yet today, etc.).
</output_instructions>
"""


class SuiteQLAgent(BaseSpecialistAgent):
    """Specialist agent for SuiteQL query construction and execution.

    Uses chain-of-thought reasoning with a strong model (Sonnet+) to understand
    user intent, explore the schema, and construct correct queries.
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
        self._soul_quirks: str = ""
        self._user_timezone: str | None = None
        self._current_task: str = ""
        self._domain_knowledge: list[str] = []

    @property
    def agent_name(self) -> str:
        return "suiteql"

    @property
    def max_steps(self) -> int:
        return 4  # query → metadata discovery if needed → corrected query → final

    @property
    def system_prompt(self) -> str:
        # Replace the placeholder with real metadata inline (inside <tenant_schema>)
        base = _SYSTEM_PROMPT
        if self._metadata:
            base = base.replace(
                "{{INJECT_CELERY_YAML_METADATA_HERE}}",
                self._build_metadata_reference(self._current_task),
            )
        else:
            base = base.replace(
                "{{INJECT_CELERY_YAML_METADATA_HERE}}",
                "(No metadata discovered yet — use ns_getSuiteQLMetadata to explore.)",
            )

        parts = [base]

        if self._domain_knowledge:
            dk_block = "\n<domain_knowledge>\nRetrieved reference material for this specific query:\n"
            for i, chunk in enumerate(self._domain_knowledge, 1):
                dk_block += f"--- Reference {i} ---\n{chunk}\n"
            dk_block += "</domain_knowledge>"
            parts.append(dk_block)

        if self._tenant_vernacular:
            parts.append("\n## EXPLICIT TENANT ENTITY RESOLUTION — MANDATORY")
            parts.append(
                "**CRITICAL**: The entities below have been pre-resolved from the user's message using fuzzy matching against this tenant's entity database. You MUST use these exact script IDs as table names or field names in your SuiteQL queries. Do NOT guess or search for alternatives."
            )
            parts.append(self._tenant_vernacular)
            parts.append(
                "\n**ACTION REQUIRED**: For each resolved entity of type 'customrecord', your FIRST query MUST be: `SELECT * FROM <internal_script_id> WHERE ROWNUM <= 5` using the netsuite_suiteql tool. Do NOT skip this step."
            )

        if self._soul_quirks:
            parts.append("\n## TENANT NETSUITE QUIRKS AND BUSINESS LOGIC")
            parts.append(
                "CRITICAL: Pay strict attention to these tenant-specific NetSuite quirks when forming queries:"
            )
            parts.append(self._soul_quirks)

        # Inject user's local date/time so date queries use correct day
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
                    f"The user's timezone is {self._user_timezone}. "
                    f"Their local date is {local_today} and local time is {local_now.strftime('%H:%M')}. "
                    f"When the user says 'today', use TO_DATE('{local_today}', 'YYYY-MM-DD'). "
                    f"When the user says 'yesterday', use TO_DATE('{local_yesterday}', 'YYYY-MM-DD')."
                )
            except Exception:
                pass  # Invalid timezone — fall back to SYSDATE behavior

        # Inject policy constraints
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
            self._tool_defs = [t for t in all_tools if t["name"] in _SUITEQL_TOOL_NAMES]
        return self._tool_defs

    async def run(
        self,
        task: str,
        context: dict[str, Any],
        db: "AsyncSession",
        adapter: "BaseLLMAdapter",
        model: str,
    ) -> AgentResult:
        """Override to dynamically add external MCP tools before running."""
        self._current_task = task
        self._tenant_vernacular = context.get("tenant_vernacular", "")
        self._user_timezone = context.get("user_timezone")
        self._domain_knowledge = context.get("domain_knowledge", [])

        try:
            from app.services.soul_service import get_soul_config

            soul_config = await get_soul_config(self.tenant_id)
            if soul_config.exists and soul_config.netsuite_quirks:
                self._soul_quirks = soul_config.netsuite_quirks
        except Exception:
            _logger.warning("suiteql_agent.soul_fetch_failed", exc_info=True)

        # Discover external MCP tools at run time (requires db session)
        try:
            from app.services.chat.tools import build_external_tool_definitions
            from app.services.mcp_connector_service import get_active_connectors_for_tenant

            connectors = await get_active_connectors_for_tenant(db, self.tenant_id)
            if connectors:
                ext_tools = build_external_tool_definitions(connectors)
                # Add ALL external NetSuite tools (SuiteQL, metadata, etc.)
                ext_ns = [t for t in ext_tools if "suiteql" in t["name"].lower() or "metadata" in t["name"].lower()]
                if ext_ns:
                    # Ensure local tools are built first
                    _ = self.tool_definitions
                    for et in ext_ns:
                        if et["name"] not in {t["name"] for t in self._tool_defs}:
                            self._tool_defs.append(et)
                    _logger.info(
                        "suiteql_agent.ext_tools_added",
                        count=len(ext_ns),
                        names=[t["name"] for t in ext_ns],
                    )
        except Exception:
            _logger.warning("suiteql_agent.ext_tool_discovery_failed", exc_info=True)

        return await super().run(task, context, db, adapter, model)

    def _build_metadata_reference(self, task: str = "") -> str:
        """Build a concise custom field reference from discovered metadata.

        Tier 1 (always): custom fields, record types, org hierarchy, lists, saved searches.
        Tier 2 (JIT): scripts, deployments, workflows — only when task mentions them.
        """
        md = self._metadata
        if md is None:
            return ""

        max_fields = 40
        parts = ["## CUSTOM FIELDS REFERENCE (discovered from this NetSuite account)"]
        parts.append("Use these field names in your queries. They are already validated.")

        # Auto-detect key lookup fields (order numbers, external refs, etc.)
        key_fields = []
        _lookup_keywords = {"order", "ext", "external", "ref", "shopify", "ecom", "channel", "source"}
        if md.transaction_body_fields and isinstance(md.transaction_body_fields, list):
            for f in md.transaction_body_fields:
                sid = (f.get("scriptid") or "").lower()
                name = (f.get("name") or "").lower()
                if any(kw in sid or kw in name for kw in _lookup_keywords):
                    key_fields.append(f)
        if key_fields:
            parts.append(
                "\n**KEY LOOKUP FIELDS** (use these when searching by external/Shopify/ecommerce order numbers):"
            )
            for f in key_fields:
                parts.append(f"  {f.get('scriptid', '?')}: {f.get('name', '?')} ({f.get('fieldtype', '?')})")

        if md.transaction_body_fields and isinstance(md.transaction_body_fields, list):
            parts.append(f"\n**Transaction body fields** ({len(md.transaction_body_fields)} total):")
            for f in md.transaction_body_fields[:max_fields]:
                linkage = ""
                if f.get("fieldtype") == "SELECT" and f.get("fieldvaluetype"):
                    linkage = f" → {f['fieldvaluetype']}"
                parts.append(f"  {f.get('scriptid', '?')} ({f.get('fieldtype', '?')}{linkage}): {f.get('name', '?')}")

        if md.transaction_column_fields and isinstance(md.transaction_column_fields, list):
            parts.append(f"\n**Transaction line fields** ({len(md.transaction_column_fields)} total):")
            for f in md.transaction_column_fields[:max_fields]:
                linkage = ""
                if f.get("fieldtype") == "SELECT" and f.get("fieldvaluetype"):
                    linkage = f" → {f['fieldvaluetype']}"
                parts.append(f"  {f.get('scriptid', '?')} ({f.get('fieldtype', '?')}{linkage}): {f.get('name', '?')}")

        if md.entity_custom_fields and isinstance(md.entity_custom_fields, list):
            parts.append(f"\n**Entity custom fields** ({len(md.entity_custom_fields)} total):")
            for f in md.entity_custom_fields[:max_fields]:
                linkage = ""
                if f.get("fieldtype") == "SELECT" and f.get("fieldvaluetype"):
                    linkage = f" → {f['fieldvaluetype']}"
                parts.append(f"  {f.get('scriptid', '?')} ({f.get('fieldtype', '?')}{linkage}): {f.get('name', '?')}")

        if md.item_custom_fields and isinstance(md.item_custom_fields, list):
            parts.append(f"\n**Item custom fields** ({len(md.item_custom_fields)} total):")
            for f in md.item_custom_fields[:max_fields]:
                linkage = ""
                if f.get("fieldtype") == "SELECT" and f.get("fieldvaluetype"):
                    linkage = f" → {f['fieldvaluetype']}"
                parts.append(f"  {f.get('scriptid', '?')} ({f.get('fieldtype', '?')}{linkage}): {f.get('name', '?')}")

        if md.custom_record_types and isinstance(md.custom_record_types, list):
            # Count total custom record fields discovered
            total_record_fields = 0
            if md.custom_record_fields and isinstance(md.custom_record_fields, list):
                total_record_fields = len(md.custom_record_fields)

            parts.append(
                f"\n**Custom record types** ({len(md.custom_record_types)} total, {total_record_fields} custom fields discovered):"
            )
            parts.append("Query custom records via: `SELECT id, ... FROM customrecord_<scriptid>`")
            parts.append("To discover fields for a custom record, use rag_search with the record name.")
            for r in md.custom_record_types[:50]:
                name = r.get("name", "?")
                scriptid = r.get("scriptid", "?")
                desc = r.get("description", "")
                desc_str = f" — {desc}" if desc and desc != name else ""
                parts.append(f"  {scriptid}: {name}{desc_str}")

        if md.subsidiaries and isinstance(md.subsidiaries, list):
            active = [s for s in md.subsidiaries if s.get("isinactive") != "T"]
            if active:
                parts.append(f"\n**Subsidiaries** ({len(active)} active):")
                for s in active:
                    parent = f" (parent: {s['parent']})" if s.get("parent") else ""
                    parts.append(f"  ID {s.get('id', '?')}: {s.get('name', '?')}{parent}")

        if md.departments and isinstance(md.departments, list):
            active = [d for d in md.departments if d.get("isinactive") != "T"]
            if active:
                parts.append(f"\n**Departments** ({len(active)} active):")
                for d in active[:20]:
                    parts.append(f"  ID {d.get('id', '?')}: {d.get('name', '?')}")

        if md.classifications and isinstance(md.classifications, list):
            active = [c for c in md.classifications if c.get("isinactive") != "T"]
            if active:
                parts.append(f"\n**Classes** ({len(active)} active):")
                for c in active[:20]:
                    parts.append(f"  ID {c.get('id', '?')}: {c.get('name', '?')}")

        if md.locations and isinstance(md.locations, list):
            active = [loc for loc in md.locations if loc.get("isinactive") != "T"]
            if active:
                parts.append(f"\n**Locations** ({len(active)} active):")
                for loc in active[:20]:
                    parts.append(f"  ID {loc.get('id', '?')}: {loc.get('name', '?')}")

        # Tier 2: Scripts, deployments, workflows — only when task mentions them
        include_scripts = bool(_SCRIPT_KEYWORDS.search(task))

        if include_scripts:
            if getattr(md, "scripts", None) and isinstance(md.scripts, list):
                parts.append(f"\n**Active Scripts** ({len(md.scripts)} total):")
                for s in md.scripts[:100]:
                    desc = f" — {s['description']}" if s.get("description") else ""
                    filepath = s.get("filepath") or s.get("scriptfile") or ""
                    file_info = f" [{filepath}]" if filepath else ""
                    parts.append(
                        f"  {s.get('scriptid', '?')} ({s.get('scripttype', '?')}): "
                        f"{s.get('name', '?')}{desc}{file_info}"
                    )

            if getattr(md, "script_deployments", None) and isinstance(md.script_deployments, list):
                parts.append(f"\n**Active Script Deployments** ({len(md.script_deployments)} total):")
                for d in md.script_deployments[:100]:
                    title = f" ({d['title']})" if d.get("title") else ""
                    event = f" [event: {d['eventtype']}]" if d.get("eventtype") else ""
                    parts.append(
                        f"  {d.get('scriptid', '?')}{title} on {d.get('recordtype', '?')} "
                        f"(Status: {d.get('status', '?')}) | Script: {d.get('script', '?')}{event}"
                    )

            if getattr(md, "workflows", None) and isinstance(md.workflows, list):
                parts.append(f"\n**Active Workflows** ({len(md.workflows)} total):")
                for w in md.workflows[:50]:
                    desc = f" — {w['description']}" if w.get("description") else ""
                    triggers = []
                    if w.get("initoncreate") == "T":
                        triggers.append("create")
                    if w.get("initonedit") == "T":
                        triggers.append("edit")
                    trigger_str = f" [triggers: {', '.join(triggers)}]" if triggers else ""
                    parts.append(
                        f"  {w.get('scriptid', '?')} on {w.get('recordtype', '?')} "
                        f"(Status: {w.get('status', '?')}): {w.get('name', '?')}{desc}{trigger_str}"
                    )
        else:
            # Summary counts so agent knows data is available on request
            script_count = len(md.scripts) if getattr(md, "scripts", None) and isinstance(md.scripts, list) else 0
            deploy_count = (
                len(md.script_deployments)
                if getattr(md, "script_deployments", None) and isinstance(md.script_deployments, list)
                else 0
            )
            wf_count = len(md.workflows) if getattr(md, "workflows", None) and isinstance(md.workflows, list) else 0
            if script_count or deploy_count or wf_count:
                parts.append(
                    f"\n(Automation metadata available but not shown: {script_count} scripts, "
                    f"{deploy_count} deployments, {wf_count} workflows. "
                    f"Ask about scripts/workflows to see details.)"
                )

        if getattr(md, "custom_list_values", None) and isinstance(md.custom_list_values, dict):
            parts.append("\n**Custom List Values** — Use exact Internal IDs for WHERE clauses instead of text:")
            parts.append("When filtering by a list field, use `WHERE field = <id>` or `BUILTIN.DF(field) = '<name>'`.")
            for list_name, values in md.custom_list_values.items():
                if values:
                    val_str = ", ".join([f"'{v.get('name')}': ID {v.get('id')}" for v in values[:50]])
                    parts.append(f"  {list_name} => {val_str}")

        if getattr(md, "saved_searches", None) and isinstance(md.saved_searches, list):
            parts.append(f"\n**Saved Searches** ({len(md.saved_searches)} public):")
            for ss in md.saved_searches[:30]:
                owner = f" (owner: {ss.get('owner', '?')})" if ss.get("owner") else ""
                parts.append(
                    f"  ID {ss.get('id', '?')}: {ss.get('title', '?')} (type: {ss.get('recordtype', '?')}){owner}"
                )

        return "\n".join(parts)
