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
from typing import TYPE_CHECKING, Any

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

# Superset of all specialist tools
_UNIFIED_TOOL_NAMES = frozenset({
    # SuiteQL agent tools
    "netsuite_suiteql",
    "netsuite_get_metadata",
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
})


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

<how_to_think>
Before taking ANY action, reason through these steps in a <reasoning> block:
1. **Understand intent**: What does the user need? Data? Documentation? Code help? Analysis?
2. **Context First**: Read all injected context blocks (<tenant_vernacular>, <domain_knowledge>, <proven_patterns>) before writing any query.
3. **Choose the right tool**: Pick the most direct tool for the job — don't over-engineer.
4. **ANTI-HALLUCINATION**: If a custom field or custom record is NOT in <tenant_schema> or <tenant_vernacular>, \
you are STRICTLY FORBIDDEN from guessing its internal ID. Use netsuite_get_metadata or rag_search to verify first.
</how_to_think>

<tool_selection>
CHOOSE THE RIGHT TOOL:

FOR DATA QUESTIONS (orders, invoices, customers, items, inventory, financial data):
→ Use netsuite_suiteql (local REST API, full permissions). This is your DEFAULT for all data queries.
→ Use netsuite_get_metadata to discover column names if unsure about schema.

FOR DOCUMENTATION / HOW-TO / ERROR LOOKUPS:
→ Use rag_search first (internal docs, custom field metadata, SuiteScript source code).
→ Use web_search as fallback for NetSuite API reference or SuiteQL syntax not in internal docs.

FOR WORKSPACE / CODE TASKS:
→ Use workspace_list_files, workspace_read_file, workspace_search, workspace_propose_patch.
→ Always read the target file before proposing changes.

FOR LEARNING / CORRECTIONS:
→ Use tenant_save_learned_rule when the user gives a standing instruction or correction.
</tool_selection>

<suiteql_dialect_rules>
SuiteQL is Oracle-based with NetSuite-specific behaviors:

PAGINATION:
- ALWAYS use `ORDER BY ... FETCH FIRST N ROWS ONLY` for "latest", "top N", or "recent" queries.
- NEVER use `WHERE ROWNUM <= N` with `ORDER BY` — ROWNUM evaluates BEFORE sorting.
- DO NOT use LIMIT — not supported in SuiteQL.

COLUMN NAMING:
- Primary key is `id` (NOT `internalid`).
- `id` is sequential — higher id = more recent. Use `ORDER BY t.id DESC` for "latest" queries.
- Transaction date: `trandate`. Created date: `createddate`.

DATE FUNCTIONS:
- For "today": `TRUNC(SYSDATE)`. For "yesterday": `TRUNC(SYSDATE) - 1`.
- For date ranges: `WHERE t.trandate >= TRUNC(SYSDATE) - 7`
- For specific dates: `WHERE t.trandate = TO_DATE('2026-01-15', 'YYYY-MM-DD')`
- NEVER use `BUILTIN.DATE(SYSDATE)` or `CURRENT_DATE`.

TEXT RESOLUTION:
- Use `BUILTIN.DF(field_name)` for List/Record fields to get display text.

HEADER vs LINE AGGREGATION — CRITICAL:
- `t.foreigntotal` and `t.total` are HEADER-LEVEL fields.
- If you JOIN transactionline, NEVER use `SUM(t.foreigntotal)` — it inflates by line count.
- For order-level totals: query `transaction` alone without transactionline.
- For line-level breakdown: use `SUM(tl.foreignamount)`.

LINE AMOUNT SIGN:
- `tl.foreignamount` is NEGATIVE for revenue lines. Use `* -1` when presenting sales totals.

MULTI-CURRENCY — CRITICAL (this tenant is multi-currency USD + EUR):
- `t.foreigntotal` = transaction currency (could be EUR, GBP, etc). NEVER use for USD totals.
- `t.total` = base/USD currency. ALWAYS use `SUM(t.total)` when user asks for "total", "revenue", or "in USD".
- ONLY use `t.foreigntotal` when user explicitly asks for per-currency breakdown with `GROUP BY BUILTIN.DF(t.currency)`.
- DEFAULT: If the user does not specify a currency, assume USD and use `t.total`.

TRANSACTION TYPES (avoid double-counting):
- For order analysis: `t.type = 'SalesOrd'` only.
- For recognized revenue: `t.type = 'CustInvc'` only.
- NEVER combine SalesOrd + CustInvc in one SUM — same sale appears as both.

ITEM TABLE GOTCHA:
- Only safe columns: id, itemid, displayname, description. Other columns may cause 0 rows.
- If a minimal query succeeds, present those results. Don't add more columns.

INVENTORY QUERIES — USE THIS PATTERN:
- ALWAYS use `inventoryitemlocations` table (NOT `inventorybalance`, NOT custom records).
- Join with `item` for item details: `JOIN item i ON i.id = iil.item`
- Key columns: `iil.quantityavailable`, `iil.quantityonhand`, `BUILTIN.DF(iil.location)`.
- Example: `SELECT i.itemid, i.displayname, BUILTIN.DF(iil.location) as location, iil.quantityavailable, iil.quantityonhand FROM inventoryitemlocations iil JOIN item i ON i.id = iil.item WHERE iil.quantityavailable > 0 ORDER BY i.itemid FETCH FIRST 100 ROWS ONLY`
- For item filtering: add `WHERE i.displayname LIKE '%keyword%'` or `WHERE i.itemid LIKE '%keyword%'`.
- If inventory query returns 0 rows, retry WITHOUT the `quantityavailable > 0` filter — items may have zero stock.
- If the JOIN still returns 0, query `item` alone first to confirm items exist, then retry `inventoryitemlocations` with explicit item IDs: `WHERE iil.item IN (id1, id2, ...)`. NetSuite REST API can occasionally return 0 rows transiently.
- DO NOT waste steps searching RAG, web_search, or custom records for inventory data — `inventoryitemlocations` is the definitive source.

CUSTOM RECORD TABLES:
- Use LOWERCASE scriptid: `customrecord_r_inv_processor`.

PREFLIGHT SCHEMA CHECK — MANDATORY:
- Before executing any query, verify ALL columns exist in <domain_knowledge>, <tenant_schema>, or <tenant_vernacular>.
- If a column is NOT in any of those sources, you MUST look it up BEFORE running the query. Use netsuite_get_metadata or web_search to verify.
- NEVER guess column names. Guessing wastes steps and budget on 400 errors.
- Standard safe columns that never need verification: id, tranid, trandate, type, entity, status, total, foreigntotal, memo, createddate (transaction); id, transaction, item, quantity, rate, amount, foreignamount, mainline, taxline, iscogs, linesequencenumber, class, department, location, quantityreceived, quantitybilled, memo, createdfrom (transactionline); id, companyname, email (customer); id, itemid, displayname, description, type (item).
- KNOWN RESTRICTED COLUMNS on transactionline via REST API (will return 400): expectedreceiptdate, itemtype. Use t.expectedreceiptdate from transaction header. Use i.type from item table for item type filtering.
</suiteql_dialect_rules>

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
</workspace_rules>

<agentic_workflow>
You are an AGENT. Run tools in a loop until you have the answer.

WORKFLOW:
1. Read all context blocks first (<tenant_vernacular>, <domain_knowledge>, <proven_patterns>).
2. Choose the right tool and execute.
3. If a tool fails, diagnose and retry with a fix (not the same call).
4. STOP when you have the answer. Don't run extra queries for "more detail".
5. Maximum budget: 6 tool calls. Use them wisely.

ERROR RECOVERY:
- "Unknown identifier" → fix column name via netsuite_get_metadata, then retry.
- 0 rows on item table → don't retry with different columns. Present what you have.
- Query syntax error → fix and retry.
- No results after 2 attempts → report clearly and suggest what info would help.
</agentic_workflow>

<output_instructions>
LANGUAGE: Always respond in English only.

Output reasoning in a <reasoning> block (hidden from user).

FORMAT RESULTS:
1. ONE sentence summarizing the result.
2. Markdown table with ALL rows — human-readable column headers.
3. Nothing else — no disclaimers, no SQL, no "let me know if you need more".

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
        self._soul_quirks: str = ""
        self._user_timezone: str | None = None
        self._current_task: str = ""
        self._domain_knowledge: list[str] = []
        self._proven_patterns: list[dict] = []

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

        # Soul quirks
        if self._soul_quirks:
            parts.append("\n## TENANT NETSUITE QUIRKS AND BUSINESS LOGIC — HIGHEST PRIORITY")
            parts.append(
                "These are the tenant's explicit field mappings and business rules. "
                "They ALWAYS take priority over conversation history and proven patterns. "
                "If a field mapping here contradicts a query from earlier in the conversation, USE THE MAPPING HERE."
            )
            parts.append(self._soul_quirks)

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

    async def run(
        self,
        task: str,
        context: dict[str, Any],
        db: "AsyncSession",
        adapter: "BaseLLMAdapter",
        model: str,
    ):
        """Override to inject context and discover external MCP tools."""
        # Augment the task with resolved entity mappings so the agent can't ignore them
        vernacular = context.get("tenant_vernacular", "")
        if vernacular:
            task = self._augment_task_with_entities(task, vernacular)
        self._current_task = task
        self._tenant_vernacular = vernacular
        self._user_timezone = context.get("user_timezone")
        self._domain_knowledge = context.get("domain_knowledge", [])
        self._proven_patterns = context.get("proven_patterns", [])

        # Load soul config
        try:
            from app.services.soul_service import get_soul_config
            soul_config = await get_soul_config(self.tenant_id)
            if soul_config.exists and soul_config.netsuite_quirks:
                self._soul_quirks = soul_config.netsuite_quirks
        except Exception:
            _logger.warning("unified_agent.soul_fetch_failed", exc_info=True)

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

        return await super().run(task, context, db, adapter, model)
