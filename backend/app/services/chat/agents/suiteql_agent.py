"""SuiteQL specialist agent — reasoning-first query generation.

Uses chain-of-thought reasoning to understand user intent, plan the query
approach, explore the schema via metadata tools, and construct correct
SuiteQL queries. Designed to work with a strong reasoning model (Sonnet+).

Prefers the local netsuite_suiteql REST API tool (OAuth 2.0, full permissions)
over the external MCP endpoint which may have restricted record type access.
"""

from __future__ import annotations

import logging
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

# Tools this agent is allowed to use
_SUITEQL_TOOL_NAMES = frozenset(
    {
        "netsuite_suiteql",
        "netsuite_get_metadata",
        "rag_search",
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
3. No Guessing (CRITICAL): If the user asks about a custom record or field that is NOT defined in the <tenant_vernacular>, you MUST use the ns_getSuiteQLMetadata tool to search for the record type FIRST. You are strictly forbidden from guessing custom table names (e.g., customrecord_celigo_integration) unless explicitly found in the metadata or <tenant_vernacular>.
4. Identify the right columns: SCAN the <tenant_schema> and <tenant_vernacular> custom fields carefully. If the user mentions an external ID, order number, Shopify reference, etc., look at the KEY LOOKUP FIELDS and custom field names/descriptions.
5. Identify tables and plan joins: What are the join keys? (e.g., transactionline tl JOIN transaction t ON tl.transaction = t.id)
6. Write ONE query: Combine all filters with OR if searching multiple fields. Do NOT write multiple queries.
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
- Filter to item lines only using `tl.mainline = 'F' AND tl.taxline = 'F'`.
- For header-only queries (no line details), use `WHERE t.mainline = 'T'` or just query the `transaction` table without joining `transactionline`.
</suiteql_dialect_rules>

<common_queries>
IMPORTANT: For simple lookups, use ONE query. Do NOT over-engineer with multiple calls.

- Transaction by number: `SELECT t.id, t.tranid, t.trandate, BUILTIN.DF(t.entity) as customer, BUILTIN.DF(t.status) as status, t.foreigntotal FROM transaction t WHERE t.tranid = 'RMA61214'`
- Order by internal ID: `SELECT ... FROM transaction t WHERE t.id = 12345`
- Latest N orders: `SELECT ... FROM transaction t WHERE t.type = 'SalesOrd' ORDER BY t.id DESC FETCH FIRST 10 ROWS ONLY`
- Customer by name: `SELECT id, companyname, email FROM customer WHERE LOWER(companyname) LIKE '%acme%'`

When a user mentions an external order number (Shopify, ecommerce, etc.), check the <tenant_schema> and <tenant_vernacular> for custom body fields that contain "order" or "ext" in their name. Search `tranid`, `otherrefnum`, AND any relevant custbody field in a single query using OR.
</common_queries>

<agentic_workflow>
You are an AGENT. Your job is to run tools in a loop until you achieve the user's goal.

STEP 0 — MATCH CUSTOM RECORDS FIRST (MANDATORY):
Before doing ANYTHING, scan the <tenant_vernacular> XML block and the <tenant_schema> **Custom record types** list.
If the user's query mentions ANY custom record by name (even partially), you MUST query that custom record table FIRST using netsuite_suiteql using the exact resolved script ID.

WORKFLOW:
1. If a custom record matched in Step 0: Use netsuite_suiteql to run `SELECT * FROM <resolved_lowercase_script_id> WHERE ROWNUM <= 5` to discover columns, then query with filters.
2. If no custom record matched and it's not in vernacular: Query standard tables (transaction, customer, item, etc.) using netsuite_suiteql (local REST API).
3. RECOVER FROM ERRORS: If a query fails with "Unknown identifier", fix the column name and retry. If it fails with syntax error, fix and retry.
4. KEEP GOING: Do NOT stop after discovering a record. Do NOT stop after finding column names. Keep going until you have DATA ROWS that answer the user's question.
5. ASK FOR HELP ONLY WHEN STUCK: Only ask the user for clarification if you've exhausted all approaches.

TOOL SELECTION — CRITICAL:
- netsuite_suiteql: Local REST API for SuiteQL (OAuth 2.0). USE THIS AS DEFAULT for ALL queries — both custom records (customrecord_*) AND standard tables (transaction, customer, item, etc.). Has full permissions.
- external_mcp_suiteql: NetSuite MCP endpoint. ONLY use as fallback if netsuite_suiteql fails. May have restricted permissions (some record types like RMA/Return Authorization may not be visible).
- netsuite_get_metadata: Discover column names for standard record types, and to safely discover the script_id of a custom record if guessing is tempting.
- tenant_save_learned_rule: When the user gives a standing instruction, correction, or preference about how queries or outputs should work (e.g., "always show Value not ID", "remember that X means Y"), call this tool to persist it for future sessions.
- rag_search: Search internal documentation.

CUSTOM RECORD TABLE NAMING — IMPORTANT:
- Custom record tables in SuiteQL use LOWERCASE scriptid: `customrecord_r_inv_processor` (not CUSTOMRECORD_R_INV_PROCESSOR)
- Always convert `<tenant_vernacular>` internal_script_id to lowercase for queries.
- Query pattern: `SELECT * FROM customrecord_<lowercase_script_id> WHERE ROWNUM <= 5`

ERROR RECOVERY:
- "Record not found" or "Invalid or unsupported search" → switch to netsuite_suiteql (local REST API) which has full permissions.
- Unknown identifier → try `SELECT * FROM <table> WHERE ROWNUM <= 1` to discover real column names, then retry.
- 0 rows returned → report "0 rows found" with the query you ran. Do NOT retry with different filters.
- Each retry MUST be meaningfully different from the previous attempt.
</agentic_workflow>

<output_instructions>
LANGUAGE: Always respond in English only. Never mix in other languages.

Output your reasoning in a <reasoning> block (this is hidden from the user).
Return ONLY the raw query results formatted as a clean, readable Markdown table.
Do NOT echo tool call parameters, JSON payloads, or SQL queries in your text output.
Do NOT interpret the data — the coordinator will handle synthesis.

If all tool calls failed or timed out, return a brief summary of what went wrong
and suggest what information the user could provide to help (e.g., "Could you confirm
the exact order number format?").
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

    @property
    def agent_name(self) -> str:
        return "suiteql"

    @property
    def max_steps(self) -> int:
        return 6  # explore schema → query → error recovery → retry → refine → final

    @property
    def system_prompt(self) -> str:
        # Replace the placeholder with real metadata inline (inside <tenant_schema>)
        base = _SYSTEM_PROMPT
        if self._metadata:
            base = base.replace(
                "{{INJECT_CELERY_YAML_METADATA_HERE}}",
                self._build_metadata_reference(),
            )
        else:
            base = base.replace(
                "{{INJECT_CELERY_YAML_METADATA_HERE}}",
                "(No metadata discovered yet — use ns_getSuiteQLMetadata to explore.)",
            )

        parts = [base]

        if self._tenant_vernacular:
            parts.append("\n## EXPLICIT TENANT ENTITY RESOLUTION — MANDATORY")
            parts.append(
                "**CRITICAL**: The entities below have been pre-resolved from the user's message using fuzzy matching against this tenant's entity database. You MUST use these exact script IDs as table names or field names in your SuiteQL queries. Do NOT guess or search for alternatives."
            )
            parts.append(self._tenant_vernacular)
            parts.append(
                "\n**ACTION REQUIRED**: For each resolved entity of type 'customrecord', your FIRST query MUST be: `SELECT * FROM <internal_script_id> WHERE ROWNUM <= 5` using the netsuite_suiteql tool. Do NOT skip this step."
            )

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
        self._tenant_vernacular = context.get("tenant_vernacular", "")

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

    def _build_metadata_reference(self) -> str:
        """Build a concise custom field reference from discovered metadata."""
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

        if getattr(md, "scripts", None) and isinstance(md.scripts, list):
            parts.append(f"\n**Active Scripts** ({len(md.scripts)} total):")
            for s in md.scripts[:100]:
                desc = f" — {s['description']}" if s.get("description") else ""
                file_info = f" (file: {s['scriptfile']})" if s.get("scriptfile") else ""
                parts.append(
                    f"  ID {s.get('id', '?')} | {s.get('scriptid', '?')} ({s.get('scripttype', '?')}): "
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
