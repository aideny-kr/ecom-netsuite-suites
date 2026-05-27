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


# ---------------------------------------------------------------------------
# Provider descriptions for connected systems awareness
# ---------------------------------------------------------------------------
_PROVIDER_DESCRIPTIONS: dict[str, str | None] = {
    "netsuite_mcp": (
        "NetSuite ERP — financial reports, saved searches, SuiteQL queries, "
        "record CRUD, and subsidiary management. Primary source of truth for "
        "transactions, GL, inventory, and customer/vendor records."
    ),
    "shopify_mcp": (
        "Shopify eCommerce — online orders, products, customers, inventory levels, "
        "and fulfillments. Use for ecommerce-specific queries."
    ),
    "stripe_mcp": (
        "Stripe Payments — charges, subscriptions, invoices, refunds, and payouts. Use for payment and billing queries."
    ),
    "custom": None,  # Falls back to connector.label
}


def _build_role_prompt(connectors: list | None, brand_name: str) -> str:
    """Build a dynamic role prompt based on connected systems.

    NetSuite-only tenants get the familiar expert prompt.
    Multi-MCP tenants get a systems-aware role.
    """
    if not connectors:
        return (
            f"You are an AI operations assistant for {brand_name}. "
            "You have access to NetSuite via SuiteQL queries and can analyze data, "
            "review SuiteScript code, and search documentation."
        )

    providers = set()
    for c in connectors:
        if c.provider == "netsuite_mcp":
            providers.add("NetSuite")
        elif c.provider == "shopify_mcp":
            providers.add("Shopify")
        elif c.provider == "stripe_mcp":
            providers.add("Stripe")
        elif c.provider == "custom":
            providers.add(c.label)

    systems_str = ", ".join(sorted(providers))

    return (
        f"You are an AI operations assistant for {brand_name}. "
        f"You have access to the following connected systems: {systems_str}. "
        "You can query data across these systems, analyze results, review code, "
        "and help with operational questions. When the user asks WHY something happened, "
        "investigate — don't just report the current value. Check automation scripts, "
        "business rules, and cross-system data flows to find the root cause."
    )


def _build_connected_systems_block(connectors: list | None) -> str:
    """Build a <connected_systems> prompt block describing each connected MCP."""
    if not connectors:
        return ""

    systems = []
    for connector in connectors:
        if not connector.discovered_tools:
            continue

        tool_names = [t.get("name", "unknown") for t in connector.discovered_tools]
        tool_count = len(tool_names)
        provider_desc = _PROVIDER_DESCRIPTIONS.get(connector.provider)
        if provider_desc is None:
            provider_desc = f"{connector.label} — custom integration."
        tool_list = ", ".join(tool_names[:15])
        if len(tool_names) > 15:
            tool_list += f" (+{len(tool_names) - 15} more)"

        systems.append(
            f"<system provider='{connector.provider}' label='{connector.label}'>\n"
            f"  Description: {provider_desc}\n"
            f"  Tools ({tool_count}): {tool_list}\n"
            f"</system>"
        )

    if not systems:
        return ""

    return (
        "\n<connected_systems>\n"
        "The following external systems are connected. "
        "Each system's tools are prefixed with 'ext__' in the tool list.\n\n"
        + "\n\n".join(systems)
        + "\n</connected_systems>"
    )


# Keywords that indicate the user's query is about scripts/automation/workflows.
_SCRIPT_KEYWORDS = re.compile(
    r"\b(?:scripts?|deploy(?:ment)?s?|workflows?|triggers?|automation|scheduled|user\s*events?|"
    r"suitelets?|restlets?|map\s*reduce|client\s*scripts?|mass\s*updates?|portlets?|"
    r"bundles?|sdf|customscript\w*)\b",
    re.IGNORECASE,
)

# Financial-mode tools — only these are exposed when handling financial queries
_FINANCIAL_TOOL_NAMES = frozenset(
    {
        "netsuite_report",
        "rag_search",
    }
)

_SYSTEM_PROMPT = """\
<role>
{{INJECT_ROLE_PROMPT}}
You combine deep knowledge of SuiteQL (Oracle-based SQL dialect), \
NetSuite documentation, SuiteScript development, and data analysis. Your job is to understand what the user \
needs and use the right tools to get the answer efficiently.
</role>

{{INJECT_CONNECTED_SYSTEMS}}

<tenant_context>
<tenant_schema>
{{INJECT_METADATA_HERE}}
</tenant_schema>
</tenant_context>

{{INJECT_TABLE_SCHEMAS}}

{{TOOL_INVENTORY}}

<tool_selection>
FINANCIAL STATEMENTS → netsuite_financial_report (local) or ns_runReport (MCP, call ns_listAllReports first).
  Parameters: report_type ("income_statement"|"balance_sheet"|"trial_balance"|"income_statement_trend"|"balance_sheet_trend"), period ("Feb 2026"), subsidiary_id (optional). ALWAYS use accounting period names, NEVER date ranges.
SAVED SEARCHES → ns_runSavedSearch (call ns_listSavedSearches to discover).
AD-HOC DATA → ns_runCustomSuiteQL (MCP, preferred) or netsuite_suiteql (local, fallback). Check <tenant_schema>, <tenant_vernacular>, <proven_patterns>, <learned_rules> before querying. Follow ALL <suiteql_dialect_rules>.
PIVOT/CROSSTAB → pivot_query_result tool (NOT manual CASE WHEN SQL). Run flat GROUP BY first, then pivot.
SCHEMA DISCOVERY → check <tenant_schema> and <standard_table_schemas> first. If missing, use netsuite_get_metadata (local) or ns_getSuiteQLMetadata (MCP). NEVER guess column names.
  CUSTOM RECORDS (customrecord_*): first query MUST be `SELECT * FROM customrecord_xxx FETCH FIRST 1 ROWS ONLY` with no custom field filters. Only use columns from the result. System date fields: `created` and `lastmodified`.
DOCS/ERRORS → rag_search first, web_search as fallback.
WORKSPACE → workspace_search → workspace_read_file → workspace_propose_patch. Always read before patching.
LEARNING → tenant_save_learned_rule for standing instructions or corrections.
</tool_selection>

<common_queries>
QUERY STRATEGY:
- LOOKUPS (specific record): simple SELECT + WHERE. One query.
- ANALYTICAL ("total sales", "breakdown by"): ALWAYS GROUP BY + aggregate. NEVER fetch individual rows to summarize.
- Max 100 rows unless user asks for full list. Summaries should be <20 rows.
- Summary AND breakdown → two separate aggregation queries.

AGGREGATION DISCIPLINE (prevents 500-row explosions):
- GROUP BY at most 2-3 dimensions. Do NOT add extra dimensions the user didn't ask for.
- >50 rows = too granular. Reduce dimensions.
- ONE query per intent. Do not run 5 variations with minor tweaks.
- YoY comparisons: ideal ~5-15 rows (dimension × year).

External order numbers (Shopify, etc.): check <tenant_vernacular> for custbody fields with "order"/"ext". Search tranid, otherrefnum, AND custbody field using OR.
</common_queries>

<workspace_rules>
CHANGE REQUEST DISCIPLINE:
- BUDGET: 3 tool calls max: workspace_search → workspace_read_file → workspace_propose_patch.
- ONE patch per file. After proposing, present result immediately — do not re-read.
- For script changes, use workspace tools — NOT NetSuite record creation.
</workspace_rules>

<agentic_workflow>
You are an AGENT. Run tools in a loop until you have the answer.

DATA FRESHNESS RULES:
1. USER-PROVIDED SQL → execute via netsuite_suiteql. NEVER answer from memory.
2. NEW DATA QUESTIONS → MUST call a tool. NEVER make up numbers.
3. TRANSFORMATION REQUESTS (chart, pivot, export existing data) → use reference_previous_result if [CACHED DATA AVAILABLE].
4. When in doubt, re-query (always safe).

CHECK CONTEXT FIRST:
- Scan <tenant_vernacular>, <tenant_schema>, <proven_patterns>, <domain_knowledge> for custom records, field mappings, and proven query patterns. Use them — do NOT invent queries when patterns exist.
- Verify ALL columns against schema before querying. Unknown columns → netsuite_get_metadata.

EXECUTE ONE QUERY — pick the right tool.

⚠️ ANTI-ENRICHMENT — READ BEFORE EVERY QUERY:
- PIVOT/CROSSTAB → Call `pivot_query_result` tool (NOT CASE WHEN SQL).
- "received RMAs" → ONE query: `WHERE t.type = 'RtnAuth' AND t.status IN ('D','E','F','G','H')`. Do NOT join item receipts.
- "received RMAs at location X" → join transactionline for location (location is on LINES, not header):
  `FROM transaction t JOIN transactionline tl ON tl.transaction = t.id AND tl.mainline = 'F' AND tl.taxline = 'F' JOIN location loc ON loc.id = tl.location WHERE t.type = 'RtnAuth' AND t.status IN ('D','E','F','G','H') AND UPPER(loc.name) LIKE '%X%'`
  NOTE: t.location (header) is often empty. Always use tl.location (line) for location filtering.
- "open POs" → ONE query with status filter. Do NOT join item receipts or vendor bills.
- "invoices this month" → ONE query with date + status filter. Do NOT join payments.
- RULE: If status codes answer the question, that IS the answer. No cross-reference joins unless the user explicitly asked for linked record details.
- NEVER join ItemRcpt to "prove" an RMA was received — the status code already tells you.

ERROR RECOVERY:
If a query fails, diagnose WHY before trying a different approach — read the error, check assumptions against schema, try a focused fix. Do not abandon a working approach after a single failure.
- "Record not found" or "Invalid or unsupported search" → switch to netsuite_suiteql (local REST API).
- "Unknown identifier" → `SELECT * FROM <table> WHERE ROWNUM <= 1` to discover columns, then retry.
- 0 rows on ITEM table → call netsuite_get_metadata. Do NOT retry with different column combos.
- 0 rows on other tables → report "0 rows found". Only retry if query logic was wrong.
After 2 failures → report clearly and suggest what info would help.

STOP WHEN YOU HAVE DATA:
Once a query returns 1+ rows answering the question, STOP. Do NOT run additional "enrichment" queries.
EXCEPTION: For "why" questions, continue investigating until root cause is found (query systemnote).

BUDGET: Data queries = 1-2 tool calls. Investigation = more to follow evidence chain. Not a data question? → rag_search first, web_search fallback.
</agentic_workflow>

<output_instructions>
Output reasoning in a <reasoning> block (hidden from user).

1. SuiteQL success → ONE sentence summary. No markdown table, JSON, or SQL (UI renders separately).
2. Financial report → markdown table grouped by section (Revenue, COGS, Expenses, etc.). Include every account row. Use ONLY the pre-computed summary totals — do NOT calculate yourself.
3. workspace_propose_patch → ```diff block + one-sentence summary.
4. 0 rows → say so clearly with possible reasons. Documentation → info with source paths. Code → fenced blocks.

CONFIDENCE SCORING:
Rate 1-5: 5=proven pattern/simple lookup, 4=successful query, 3=may be incomplete, 2=uncertain after retries, 1=guessing.
Output: <confidence>N</confidence> (parsed and logged).
</output_instructions>
"""

_INVESTIGATION_OUTPUT_INSTRUCTIONS = (
    "<output_instructions>\n"
    "LANGUAGE: Always respond in English unless the user asks in another language "
    "but do not get the language mixed when output.\n\n"
    "Present your findings progressively as you investigate.\n"
    "After each tool result, share what you learned before continuing.\n"
    "Build a chronological narrative — explain what happened, when, and why.\n"
    "When you've found the root cause, present a clear summary.\n\n"
    "CONFIDENCE SCORING:\n"
    "Before your final answer, rate your confidence (1-5):\n"
    "5 = Clear root cause found with evidence\n"
    "4 = Strong evidence, minor gaps\n"
    "3 = Partial evidence, some assumptions\n"
    "2 = Limited evidence, uncertain conclusion\n"
    "1 = Guessing, insufficient data\n"
    "Rate based on EVIDENCE QUALITY, not number of steps taken. "
    "More tool calls to gather evidence = thoroughness, not uncertainty.\n"
    "Output: <confidence>N</confidence> in your response (this tag is parsed and logged).\n"
    "</output_instructions>"
)

_SYSTEMNOTE_EXPERTISE = (
    "\n<systemnote_expertise>\n"
    "To investigate 'why' questions, query the systemnote table:\n"
    "- Filter: recordtypeid = -30 (transactions), recordid = <internal_id>\n"
    "- BUILTIN.DF(sn.field) does NOT work (static list error) — read raw field names\n"
    "- Field names use internal notation: TRANDOC.KSTATUS (status), CUSTBODY_* (custom body fields)\n"
    "- Infer meaning from naming conventions: CUSTBODY_FW_HOLD_EDI_TRANSMIT = EDI hold flag\n"
    "- context column: SLT=Suitelet, MPR=Map/Reduce, UIF=User Interface, CSV=Import\n"
    "- name = -4 means system/script action, positive numbers are user IDs\n"
    "- Order results by date ASC for chronological narrative\n"
    "- When asked about durations or 'how long', calculate EXACT time differences from timestamps. "
    "Do not approximate (e.g., say '22 hours 14 minutes' not '~1 day').\n"
    "</systemnote_expertise>\n"
)


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
        context_need: str = "FULL",
    ) -> None:
        super().__init__(tenant_id, user_id, correlation_id)
        self._metadata = metadata
        self._policy = policy
        self._context_need = context_need
        self._tool_defs: list[dict] | None = None
        self._tenant_vernacular: str = ""
        self._onboarding_profile: str = ""
        self._soul_quirks: str = ""
        self._soul_tone: str = ""
        self._brand_name: str = ""
        self._netsuite_account_slug: str = ""  # e.g. "1234567" for URL construction
        self._user_timezone: str | None = None
        self._current_task: str = ""
        self._domain_knowledge: list[str] = []
        self._proven_patterns: list[dict] = []
        self._active_skill: dict | None = None  # Set when a skill is triggered
        self._context: dict[str, Any] = {}  # Full context dict from orchestrator
        self._connectors: list = []  # Active MCP connectors for this tenant
        # Plan Mode injections — set per-turn by the orchestrator. Empty by default.
        # `_plan_mode_augmentation`: appended to system prompt when financial-ambiguity
        #   regex matches AND plan_mode flag is on (initial gate intent).
        # `_plan_mode_resume_directive`: appended AFTER augmentation so it overrides
        #   initial gate intent on resume turns (after the user picks an option).
        # The orchestrator USED to mutate a local `system_prompt` variable, but the
        # UnifiedAgent.system_prompt property builds its own prompt and never read
        # that local — so the augmentations were dead code. Setting them on the
        # instance routes them through this property where the LLM can see them.
        self._plan_mode_augmentation: str = ""
        self._plan_mode_resume_directive: str = ""

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
        # Investigation queries get more budget to follow evidence chains
        return 40 if self._context_need == "full" else 12

    @property
    def system_prompt(self) -> str:
        base = _SYSTEM_PROMPT

        # Inject dynamic role prompt based on connected systems
        role_prompt = _build_role_prompt(
            self._connectors if self._connectors else None,
            self._brand_name or "Suite Studio AI",
        )
        base = base.replace("{{INJECT_ROLE_PROMPT}}", role_prompt)

        # Inject connected systems block
        systems_block = _build_connected_systems_block(
            self._connectors if self._connectors else None,
        )
        base = base.replace("{{INJECT_CONNECTED_SYSTEMS}}", systems_block)

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

        # ── Investigation mode: strip verbose sections so the LLM can think freely ──
        # Claude + native MCP solves investigation queries perfectly with zero SuiteQL rules.
        # Our 400-line prompt drowns the investigation hints in noise. Strip it down.
        if self._context_need == "full":
            # Remove entire XML blocks that add noise for investigation
            for tag in (
                "common_queries",  # anti-enrichment, aggregation discipline, item gotchas
                "workspace_rules",  # SuiteScript rules — not relevant
                "rag_search_tips",  # search tips — not relevant
            ):
                base = re.sub(rf"<{tag}>.*?</{tag}>", "", base, flags=re.DOTALL)

            # Replace heavy suiteql_dialect_rules with minimal essentials
            base = re.sub(
                r"<suiteql_dialect_rules>.*?</suiteql_dialect_rules>",
                (
                    "<suiteql_dialect_rules>\n"
                    "SuiteQL essentials:\n"
                    "- Pagination: FETCH FIRST N ROWS ONLY (not LIMIT).\n"
                    "- Dates: TO_DATE('2026-01-15', 'YYYY-MM-DD'), TRUNC(SYSDATE) for today.\n"
                    "- Booleans: 'T' = true, 'F' = false.\n"
                    "- Display values: BUILTIN.DF(field) for list/record fields.\n"
                    "- Primary key: id (not internalid). Higher id = more recent.\n"
                    "- Status codes: single-letter only (e.g. 'B'), never compound ('SalesOrd:B').\n"
                    "</suiteql_dialect_rules>"
                ),
                base,
                flags=re.DOTALL,
            )

            # Replace verbose agentic_workflow with lean investigation workflow
            base = re.sub(
                r"<agentic_workflow>.*?</agentic_workflow>",
                (
                    "<agentic_workflow>\n"
                    "You are investigating a 'why' question. Follow the evidence:\n"
                    "1. Find the record and get its internal ID.\n"
                    "2. Query systemnote for that record to see all field changes.\n"
                    "3. Analyze the changes chronologically to explain the ROOT CAUSE.\n"
                    "</agentic_workflow>"
                ),
                base,
                flags=re.DOTALL,
            )

            # Slim down tool_selection — keep only SuiteQL and schema discovery
            base = re.sub(
                r"<tool_selection>.*?</tool_selection>",
                (
                    "<tool_selection>\n"
                    "Use MCP ns_runCustomSuiteQL (preferred) or local netsuite_suiteql for data queries.\n"
                    "Check <tenant_schema> for valid column names before querying.\n"
                    "Use netsuite_get_metadata for column discovery if needed.\n"
                    "</tool_selection>"
                ),
                base,
                flags=re.DOTALL,
            )

            # Replace output_instructions with progressive investigation format
            base = re.sub(
                r"<output_instructions>.*?</output_instructions>",
                _INVESTIGATION_OUTPUT_INSTRUCTIONS,
                base,
                flags=re.DOTALL,
            )

            # Add systemnote expertise at the end (highest attention per U-curve research)
            base += _SYSTEMNOTE_EXPERTISE

            print("[UNIFIED] Investigation mode: prompt stripped for free reasoning", flush=True)

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

        # NetSuite record deep links
        if self._netsuite_account_slug:
            parts.append(
                f"\n<record_links>\n"
                f"When referencing NetSuite records, include a clickable link using this pattern:\n"
                f"Base URL: https://{self._netsuite_account_slug}.app.netsuite.com\n\n"
                f"| Record Type | Path |\n"
                f"|---|---|\n"
                f"| Invoice | /app/accounting/transactions/custinvc.nl?id={{id}} |\n"
                f"| Sales Order | /app/accounting/transactions/salesord.nl?id={{id}} |\n"
                f"| Purchase Order | /app/accounting/transactions/purchord.nl?id={{id}} |\n"
                f"| Vendor Bill | /app/accounting/transactions/vendbill.nl?id={{id}} |\n"
                f"| Customer Payment | /app/accounting/transactions/custpymt.nl?id={{id}} |\n"
                f"| Journal Entry | /app/accounting/transactions/journal.nl?id={{id}} |\n"
                f"| Credit Memo | /app/accounting/transactions/credmemo.nl?id={{id}} |\n"
                f"| Customer | /app/common/entity/custjob.nl?id={{id}} |\n"
                f"| Vendor | /app/common/entity/vendor.nl?id={{id}} |\n"
                f"| Employee | /app/common/entity/employee.nl?id={{id}} |\n\n"
                f"Always include t.id in SELECT when querying transactions so you can construct links.\n"
                f"Format: [Invoice #{{tranid}}](full_url) or [{{customer_name}}](full_url)\n"
                f"</record_links>"
            )

        # Learned rules — tenant-specific business logic (always injected)
        _learned_rules = self._context.get("learned_rules", [])
        if _learned_rules:
            lr_block = "\n<learned_rules>\nTenant-specific business rules — FOLLOW THESE STRICTLY:\n"
            for rule in _learned_rules:
                lr_block += f"  [{rule['category']}] {rule['description']}\n"
            lr_block += "</learned_rules>"
            parts.append(lr_block)

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

        # Current date — ALWAYS injected via the shared helper so the LLM
        # never has to guess from its training cutoff. SpecializedAgent uses
        # the same helper; keep them in sync via that single source.
        from app.services.chat.agents.base_agent import build_current_date_block

        date_block = build_current_date_block(self._user_timezone)
        if date_block:
            parts.append(date_block)

        # Fiscal calendar — tell the LLM how to interpret Q1/Q2/Q3/Q4 and
        # "fiscal year" references. Without this it defaults to calendar year.
        fy_start = self._context.get("fiscal_year_start_month", 1) if self._context else 1
        if fy_start and fy_start != 1:
            _month_names = [
                "January",
                "February",
                "March",
                "April",
                "May",
                "June",
                "July",
                "August",
                "September",
                "October",
                "November",
                "December",
            ]
            _start_name = _month_names[fy_start - 1]
            _q1_end = _month_names[(fy_start + 2) % 12]
            _q2_start = _month_names[(fy_start + 3) % 12]
            _q2_end = _month_names[(fy_start + 5) % 12]
            _q3_start = _month_names[(fy_start + 6) % 12]
            _q3_end = _month_names[(fy_start + 8) % 12]
            _q4_start = _month_names[(fy_start + 9) % 12]
            _q4_end = _month_names[(fy_start + 11) % 12]
            parts.append(
                f"\n## FISCAL CALENDAR\n"
                f"This tenant's fiscal year starts in **{_start_name}** (month {fy_start}).\n"
                f"- Fiscal Q1 = {_start_name} – {_q1_end}\n"
                f"- Fiscal Q2 = {_q2_start} – {_q2_end}\n"
                f"- Fiscal Q3 = {_q3_start} – {_q3_end}\n"
                f"- Fiscal Q4 = {_q4_start} – {_q4_end}\n"
                f"**Default behavior**: when the user says 'Q1', 'Q2', 'this quarter', 'fiscal year', "
                f"or 'YTD' without specifying 'calendar', use the FISCAL calendar above. "
                f"Only use calendar quarters if the user explicitly says 'calendar Q1', 'Jan-Mar', etc."
            )
        else:
            parts.append(
                "\n## FISCAL CALENDAR\n"
                "This tenant uses the **calendar year** (Jan 1 – Dec 31) as its fiscal year. "
                "Q1 = Jan-Mar, Q2 = Apr-Jun, Q3 = Jul-Sep, Q4 = Oct-Dec."
            )

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

        # Plan Mode injections — augmentation first, resume directive last
        # so the resume directive (chosen-option turn) overrides the initial
        # gate intent (financial-ambiguity turn) when both fire.
        if self._plan_mode_augmentation:
            parts.append("\n\n" + self._plan_mode_augmentation)
        if self._plan_mode_resume_directive:
            parts.append("\n\n" + self._plan_mode_resume_directive)

        prompt = "\n".join(parts)

        # Resolve {{TOOL_INVENTORY}} with the real tool schema.
        # Lazy import to avoid circular: orchestrator imports unified_agent.
        from app.services.chat.orchestrator import _assemble_system_prompt

        return _assemble_system_prompt(template=prompt, tool_definitions=self._tool_defs or [])

    @property
    def tool_definitions(self) -> list[dict]:
        if self._tool_defs is None:
            self._tool_defs = build_local_tool_definitions()
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

        # Build the full tool schema: connector-gated local tools + external MCP tools.
        # Use build_all_tool_definitions (not build_local_tool_definitions) so the agent's
        # prompt inventory matches the schema actually sent to the LLM — no drift.
        try:
            from app.services.chat.tools import build_all_tool_definitions
            from app.services.mcp_connector_service import get_active_connectors_for_tenant

            self._connectors = await get_active_connectors_for_tenant(db, self.tenant_id) or []
            self._tool_defs = await build_all_tool_definitions(db, self.tenant_id)
        except Exception:
            _logger.warning("unified_agent.tool_discovery_failed", exc_info=True)
            # Fallback: at least populate local tools so basic queries still work.
            if self._tool_defs is None:
                self._tool_defs = build_local_tool_definitions()

        # Extract NetSuite account slug for record deep links
        try:
            from app.core.encryption import decrypt_credentials

            for conn in self._connectors:
                if conn.provider in ("netsuite_mcp", "netsuite") and conn.encrypted_credentials:
                    creds = decrypt_credentials(conn.encrypted_credentials)
                    raw_id = creds.get("account_id", "")
                    if raw_id:
                        self._netsuite_account_slug = raw_id.replace("_", "-").lower()
                        break
        except Exception:
            _logger.warning("unified_agent.account_slug_failed", exc_info=True)

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
        plan_mode_clarify_only: bool = False,
        plan_mode_resume_source: str | None = None,
    ):
        """Override to inject context and discover external MCP tools.

        ``plan_mode_clarify_only`` filters ``self._tool_defs`` to the
        ``clarify`` tool only — applied AFTER ``_setup_context`` so the
        rebuild inside setup doesn't clobber the gate. Mirrors the
        ``financial_mode`` pattern.

        ``plan_mode_resume_source`` filters ``self._tool_defs`` to only the
        chosen source's tools (plus cross-source tools) on the resume turn
        after the user picks a clarification option. Applied AFTER
        ``_setup_context`` and the ``plan_mode_clarify_only`` filter.
        """
        task = await self._setup_context(task, context, db)
        if financial_mode:
            self._tool_defs = self.financial_tool_definitions
        if plan_mode_clarify_only:
            # Inject the canonical clarify schema unconditionally. ``_setup_context``
            # rebuilds ``_tool_defs`` via ``build_all_tool_definitions`` WITHOUT
            # ``plan_mode_enabled=True``, so the rebuild may not include clarify.
            # A naive filter would yield ``[]`` and the provider would receive
            # ``tool_choice=clarify`` with no clarify schema → silent gate failure.
            from app.services.chat.plan_mode.clarify_tool import CLARIFY_TOOL_SCHEMA

            self._tool_defs = [dict(CLARIFY_TOOL_SCHEMA)]
        if plan_mode_resume_source:
            from app.services.chat.plan_mode.short_circuit import (
                filter_tools_for_chosen_source,
            )

            self._tool_defs = filter_tools_for_chosen_source(
                self._tool_defs or [],
                plan_mode_resume_source,
                active_connectors=self._connectors,
            )
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
        plan_mode_clarify_only: bool = False,
        plan_mode_resume_source: str | None = None,
        tool_result_interceptor: Callable[[str, str], tuple[tuple[str, dict] | None, str]] | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
    ):
        """Override to inject context before streaming.

        ``plan_mode_clarify_only`` filters ``self._tool_defs`` to the
        ``clarify`` tool only — applied AFTER ``_setup_context`` so the
        rebuild inside setup doesn't clobber the gate.

        ``plan_mode_resume_source`` filters ``self._tool_defs`` to only the
        chosen source's tools (plus cross-source tools) on the resume turn
        after the user picks a clarification option. Applied AFTER
        ``_setup_context`` and the ``plan_mode_clarify_only`` filter.
        """
        task = await self._setup_context(task, context, db)
        if financial_mode:
            self._tool_defs = self.financial_tool_definitions
        if plan_mode_clarify_only:
            # Inject the canonical clarify schema unconditionally. See ``run`` above
            # for the full rationale — TL;DR ``_setup_context``'s rebuild may not
            # include clarify, so a naive filter would silently disable the gate.
            from app.services.chat.plan_mode.clarify_tool import CLARIFY_TOOL_SCHEMA

            self._tool_defs = [dict(CLARIFY_TOOL_SCHEMA)]
        if plan_mode_resume_source:
            from app.services.chat.plan_mode.short_circuit import (
                filter_tools_for_chosen_source,
            )

            self._tool_defs = filter_tools_for_chosen_source(
                self._tool_defs or [],
                plan_mode_resume_source,
                active_connectors=self._connectors,
            )
        async for event in super().run_streaming(
            task,
            context,
            db,
            adapter,
            model,
            conversation_history,
            tool_choice=tool_choice,
            tool_result_interceptor=tool_result_interceptor,
            session_id=session_id,
            run_id=run_id,
        ):
            yield event
