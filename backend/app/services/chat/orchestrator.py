"""Agentic chat orchestrator using a multi-provider LLM adapter layer.

Supports Anthropic, OpenAI, and Gemini via the adapter pattern.
Claude decides which tools to call, sees results (including errors),
and can retry/correct — all within a single turn, up to MAX_STEPS iterations.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.chat.prompt_cache import split_system_prompt

# Regex to strip leaked Anthropic tool-call XML from assistant text
_TOOL_XML_RE = re.compile(r"</?(?:invoke|parameter|tool_use)[^>]*>", re.DOTALL)
_TOOL_TAG_RE = re.compile(r"\s*\[tool:\s*[^\]]+\]")
_REASONING_RE = re.compile(r"<reasoning>.*?</reasoning>\s*", re.DOTALL)

# Chitchat regex — matches greetings, compliments, affirmations, farewells.
# Short-circuits expensive context assembly (entity resolution, domain knowledge, etc.)
_FINANCIAL_MODE_TAG = "FINANCIAL REPORT MODE"


def _build_financial_mode_task(user_message: str) -> str:
    """Build task for financial report queries.

    Always uses the LOCAL netsuite_financial_report tool which uses BUILTIN.CONSOLIDATE
    for correct multi-currency consolidation at posting-time exchange rates.
    MCP ns_runReport uses real-time FX rates which diverge from NetSuite UI numbers
    on multi-currency tenants — not suitable for penny-perfect financial statements.
    """
    return (
        f"{user_message}\n\n"
        f"[{_FINANCIAL_MODE_TAG}] Use the LOCAL netsuite_financial_report tool.\n\n"
        "Parameters:\n"
        '  report_type: "income_statement" | "balance_sheet" | "trial_balance" | "income_statement_trend" | "balance_sheet_trend"\n'
        '  period: "Feb 2026" (single) or "Jan 2026, Feb 2026, Mar 2026" (trend/quarter)\n'
        "  subsidiary_id: number (optional, use <tenant_vernacular> to resolve names to IDs)\n\n"
        "For quarters: use income_statement_trend with all 3 months.\n\n"
        "The financial report tool renders data directly in a visual table component.\n"
        "Do NOT format tables yourself. Do NOT rebuild or reproduce the data as markdown tables.\n"
        "Instead, provide COMMENTARY ONLY:\n"
        "1. A brief summary of the key findings\n"
        "2. Notable trends or anomalies\n"
        "3. Comparisons if the user asked for them\n"
        "Reference the pre-computed summary numbers (total_revenue, gross_profit, net_income, etc.) for your analysis.\n"
        "For trend reports, summary.by_period contains per-period breakdowns — compare across periods.\n\n"
        "FALLBACK: MCP ns_runReport if local tool errors (note: MCP may show slight FX differences)."
    )


_CHITCHAT_FILLER = r"(?:\s+(?:bro|man|dude|mate|buddy|guys?|y'?all|there))?"
_CHITCHAT_SEP = r"[!.\s,]*"
_CHITCHAT_RE = re.compile(
    rf"""(?xi)^[\s]*(?:
        (?:thanks?|thank\s*you|thx|ty|cheers)\b{_CHITCHAT_FILLER}{_CHITCHAT_SEP} |
        (?:great|nice|awesome|perfect|amazing|cool|excellent|wonderful|fantastic|brilliant)\b{_CHITCHAT_FILLER}{_CHITCHAT_SEP} |
        (?:good\s*job|well\s*done|nice\s*work|nailed\s*it)\b{_CHITCHAT_FILLER}{_CHITCHAT_SEP} |
        (?:you(?:'re|\s+are)\s+(?:the\s+)?(?:best|great|awesome|perfect|amazing))\b{_CHITCHAT_FILLER}{_CHITCHAT_SEP} |
        (?:(?:you\s+)?rock|love\s+(?:it|this|you)|bravo)\b{_CHITCHAT_FILLER}{_CHITCHAT_SEP} |
        (?:wow|lol|(?:ha){{2,}}|ok(?:ay)?|sure|yep|nope|got\s*it|understood|i\s*see|makes?\s*sense)\b{_CHITCHAT_FILLER}{_CHITCHAT_SEP} |
        (?:hi|hello|hey|good\s*(?:morning|afternoon|evening|night))\b{_CHITCHAT_FILLER}{_CHITCHAT_SEP} |
        (?:bye|goodbye|see\s*ya|later|gn)\b{_CHITCHAT_FILLER}{_CHITCHAT_SEP}
    ){{1,5}}[\s!.]*$""",
)


# ---------------------------------------------------------------------------
# Simple lookup detection — route to Haiku for 10x faster, 10x cheaper
# ---------------------------------------------------------------------------

HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Patterns that indicate a simple single-entity lookup or count
_SIMPLE_LOOKUP_RE = re.compile(
    r"(?i)^(?:"
    r"(?:show|find|get|look\s*up|pull|what(?:'s|\s+is))\s+(?:me\s+)?(?:the\s+)?"
    r"(?:(?:status|details?)\s+(?:of|for)\s+)?"
    r"(?:(?:item|order|invoice|customer|vendor|po|so|rma|inv|vb)\s+)?"
    r"[\w\-#][\w\-#\s]{2,30}"  # ID/SKU/number or name (up to 30 chars)
    r"|(?:how\s+many|count|total\s+number\s+of)\s+(?:open\s+|active\s+|pending\s+)?"
    r"(?:sales\s+orders?|purchase\s+orders?|invoices?|rmas?|pos?|sos?|items?|customers?|vendors?)"
    r"|(?:SO|PO|RMA|INV|VB|WO|TO|IF|IR)[\s\-#]?\d+"
    r"|#\d+"
    r")\s*\??$",
)

# Patterns that indicate complex analysis — should NOT use Haiku
_COMPLEX_QUERY_RE = re.compile(
    r"(?i)(?:compar|vs\.?|versus|trend|month.over.month|year.over|breakdown|analyz|analysis"
    r"|all\s+\w+\s+with\s+their|why\s+|how\s+do\s+i|explain|create|write|fix|deploy"
    r"|refactor|p\s*&\s*l|income\s+statement|balance\s+sheet)",
)


def _is_simple_lookup(query: str) -> bool:
    """Detect simple single-entity lookups or counts.

    Conservative: only matches clearly simple queries. Returns False
    when uncertain — better to use Sonnet than give a bad Haiku answer.
    """
    query = query.strip()
    if not query or len(query) > 120:
        return False
    if _COMPLEX_QUERY_RE.search(query):
        return False
    return bool(_SIMPLE_LOOKUP_RE.match(query))


# ---------------------------------------------------------------------------
# Connection health check — prevent wasted tool calls on dead connections
# ---------------------------------------------------------------------------

_BROKEN_STATUSES = frozenset({"needs_reauth", "error", "expired"})

# Local tools that require a healthy REST API connection
_REST_TOOLS = frozenset({"netsuite_suiteql", "netsuite_financial_report"})


async def _check_connection_health(
    db: AsyncSession, tenant_id: uuid.UUID
) -> list[str]:
    """Check REST API and MCP connection health for a tenant.

    Returns a list of warning strings for broken connections.
    Fail-open: returns [] on any DB error so chat is never blocked.
    """
    try:
        from sqlalchemy import select as sa_select

        from app.models.connection import Connection
        from app.models.mcp_connector import McpConnector

        warnings: list[str] = []

        # Check REST API connections
        conn_result = await db.execute(
            sa_select(Connection.label, Connection.status, Connection.error_reason)
            .where(Connection.tenant_id == tenant_id)
            .where(Connection.provider == "netsuite")
        )
        for conn in conn_result.all():
            if conn.status in _BROKEN_STATUSES:
                warnings.append(f"REST API ({conn.label}): {conn.status}")

        # Check MCP connections
        mcp_result = await db.execute(
            sa_select(McpConnector.label, McpConnector.status)
            .where(McpConnector.tenant_id == tenant_id)
            .where(McpConnector.is_enabled == True)  # noqa: E712
        )
        for mcp in mcp_result.all():
            if mcp.status in _BROKEN_STATUSES:
                warnings.append(f"MCP ({mcp.label}): {mcp.status}")

        return warnings
    except Exception:
        # Fail-open — don't block chat if health check fails
        return []


def _filter_tools_for_dead_connections(
    tool_definitions: list[dict], connection_warnings: list[str]
) -> list[dict]:
    """Remove tools whose backing connection is broken.

    - REST dead → strip local netsuite_suiteql, netsuite_financial_report
    - MCP dead → strip all ext__ prefixed tools
    """
    if not connection_warnings:
        return tool_definitions

    rest_dead = any("REST API" in w for w in connection_warnings)
    mcp_dead = any("MCP" in w for w in connection_warnings)

    filtered = []
    for t in tool_definitions:
        name = t["name"]
        if rest_dead and name in _REST_TOOLS:
            continue
        if mcp_dead and name.startswith("ext__"):
            continue
        filtered.append(t)
    return filtered


def _build_connection_warning_block(connection_warnings: list[str]) -> str:
    """Build a system prompt block warning the agent about broken connections."""
    if not connection_warnings:
        return ""

    warning_lines = "\n".join(f"  - {w}" for w in connection_warnings)
    return (
        f"\n\n⚠️ CONNECTION STATUS — BROKEN:\n{warning_lines}\n"
        "IMMEDIATELY tell the user which connections are down. "
        "Do NOT attempt queries against broken connections — they will fail. "
        "Direct user to Settings > Connections to re-authorize."
    )


# ---------------------------------------------------------------------------
# Smart context injection — classify how much context each query needs
# ---------------------------------------------------------------------------

class ContextNeed:
    """How much dynamic context to inject into the agent prompt."""
    FULL = "full"           # Custom fields, complex joins — inject everything
    DATA = "data"           # Standard tables, no custom fields — skip onboarding profile
    DOCS = "docs"           # Documentation question — skip schemas, inject RAG only
    WORKSPACE = "workspace" # Script question — skip all NetSuite schemas
    FINANCIAL = "financial" # Financial report — inject only vernacular + onboarding


_WORKSPACE_RE = re.compile(
    r"\b(?:scripts?|deploy(?:ment)?s?|triggers?|automation|scheduled|user\s*events?|"
    r"suitelets?|restlets?|map\s*reduce|client\s*scripts?|mass\s*updates?|portlets?|"
    r"bundles?|sdf|customscript\w*|fix\s+(?:the|my|this)\s+\w*script|write\s+a?\s*(?:suite)?script|"
    r"workflow\s*action\s*scripts?)\b",
    re.IGNORECASE,
)

_FINANCIAL_RE = re.compile(
    r"\b(?:p\s*&?\s*l|profit\s*(?:and|&)\s*loss|income\s*statement|balance\s*sheet|"
    r"trial\s*balance|aging|financial\s*(?:report|statement))\b",
    re.IGNORECASE,
)

_DOCS_RE = re.compile(
    r"\b(?:how\s+(?:do|does|can|should|to)|what\s+is|what\s+are|explain|documentation|"
    r"workflow|error\s+(?:message|code)|tutorial)\b",
    re.IGNORECASE,
)

_DATA_KEYWORDS = re.compile(
    r"(?:\b(?:show|list|find|get|pull|fetch|look\s*up|search|query|"
    r"how\s+many|total|count|sum|average|revenue|sales|orders?|"
    r"inventory|customers?|vendors?|invoices?|purchase\s*orders?)\b"
    r"|(?:^|\s)(?:RMA|PO|SO|INV|VB|WO|TO|IF|IR|#)\d)",
    re.IGNORECASE,
)


def _classify_context_need(user_message: str, is_financial: bool = False) -> str:
    """Classify how much dynamic context a query needs.

    Returns one of ContextNeed.FULL/DATA/DOCS/WORKSPACE/FINANCIAL.
    When uncertain, returns FULL — never risk under-injecting.
    """
    if is_financial:
        return ContextNeed.FINANCIAL

    msg = user_message.strip()

    # Workspace: script/deploy/SuiteScript keywords
    if _WORKSPACE_RE.search(msg):
        # But if it also has data keywords, it might be mixed — use FULL
        if _DATA_KEYWORDS.search(msg):
            return ContextNeed.FULL
        return ContextNeed.WORKSPACE

    # Docs: how-to/explain/documentation without data keywords
    if _DOCS_RE.search(msg):
        if _DATA_KEYWORDS.search(msg):
            return ContextNeed.FULL  # Mixed intent — safe fallback
        return ContextNeed.DOCS

    # Data: most common path — orders, inventory, revenue, etc.
    if _DATA_KEYWORDS.search(msg):
        return ContextNeed.DATA

    # Uncertain — always default to FULL
    return ContextNeed.FULL


def _sanitize_assistant_text(text: str) -> str:
    """Remove leaked tool-call XML, tool tags, and reasoning blocks from assistant response text."""
    text = _TOOL_XML_RE.sub("", text)
    text = _TOOL_TAG_RE.sub("", text)
    text = _REASONING_RE.sub("", text)
    return text.strip()


_FINANCIAL_TOOLS = frozenset({"netsuite.financial_report", "netsuite_financial_report"})
_SUITEQL_TOOLS = frozenset({"netsuite.suiteql", "netsuite_suiteql"})


def _is_financial_tool(tool_name: str) -> bool:
    """Match local financial tools and external MCP runReport tools."""
    if tool_name in _FINANCIAL_TOOLS:
        return True
    if tool_name.startswith("ext__") and "runreport" in tool_name.lower():
        return True
    return False


def _is_suiteql_tool(tool_name: str) -> bool:
    """Match local SuiteQL tools and external MCP SuiteQL tools."""
    if tool_name in _SUITEQL_TOOLS:
        return True
    if tool_name.startswith("ext__") and "suiteql" in tool_name.lower():
        return True
    return False


def _intercept_tool_result(
    tool_name: str, result_str: str
) -> tuple[str | None, dict | None, str]:
    """Intercept data-producing tool results for frontend DataFrame rendering.

    Returns ``(event_type, sse_event_data, result_str_for_llm)``.
    - Financial reports → ``("financial_report", {...}, condensed)``
    - SuiteQL queries  → ``("data_table", {...}, condensed)``
    - Everything else  → ``(None, None, original_result_str)``
    """

    # --- Financial report path ---
    if _is_financial_tool(tool_name):
        try:
            parsed = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            return None, None, result_str
        if not parsed.get("success"):
            return None, None, result_str

        rows = parsed.get("items", [])
        sse_event_data = {
            "report_type": parsed.get("report_type"),
            "period": parsed.get("period"),
            "columns": parsed.get("columns", []),
            "rows": rows,
            "summary": parsed.get("summary"),
        }
        condensed = json.dumps(
            {
                "success": True,
                "report_type": parsed.get("report_type"),
                "period": parsed.get("period"),
                "total_rows": len(rows),
                "summary": parsed.get("summary"),
                "note": (
                    "The full table has been sent to the frontend for rendering. "
                    "Do NOT rebuild or reproduce the table in your response. "
                    "Provide commentary and analysis only."
                ),
            },
            default=str,
        )
        return "financial_report", sse_event_data, condensed

    # --- SuiteQL query path ---
    if _is_suiteql_tool(tool_name):
        try:
            parsed = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            return None, None, result_str

        # Error results pass through
        if isinstance(parsed, dict) and (
            parsed.get("error") is True or isinstance(parsed.get("error"), str)
        ):
            return None, None, result_str

        columns = parsed.get("columns")
        rows = parsed.get("rows")

        # Handle external MCP format: {"data"|"items": [{col: val, ...}, ...]}
        if not isinstance(columns, list) or not isinstance(rows, list):
            # External MCP uses "data" or "items" key for list-of-dicts
            items = None
            if isinstance(parsed, dict):
                items = parsed.get("data") or parsed.get("items")
            if isinstance(items, list) and len(items) > 0 and isinstance(items[0], dict):
                # Derive columns from union of all item keys (preserving order)
                seen: set[str] = set()
                columns = []
                for item in items:
                    for key in item:
                        if key not in seen:
                            seen.add(key)
                            columns.append(key)
                rows = [[item.get(col) for col in columns] for item in items]
            else:
                return None, None, result_str

        row_count = parsed.get("row_count") or parsed.get("resultCount") or len(rows)
        query = parsed.get("query") or parsed.get("queryExecuted") or ""
        truncated = parsed.get("truncated", False)

        sse_event_data = {
            "columns": columns,
            "rows": rows,
            "row_count": row_count,
            "query": query,
            "truncated": truncated,
        }
        condensed = json.dumps(
            {
                "columns": columns,
                "row_count": row_count,
                "truncated": truncated,
                "note": (
                    "The full data table has been sent to the frontend for rendering. "
                    "Do NOT rebuild or reproduce the table in your response. "
                    "Provide commentary, insights, and analysis only."
                ),
            },
            default=str,
        )
        return "data_table", sse_event_data, condensed

    # --- Not a data tool ---
    return None, None, result_str


def _tool_interceptor(tool_name: str, result_str: str) -> tuple[tuple[str, dict] | None, str]:
    """Adapter: wraps _intercept_tool_result for the agent callback interface."""
    event_type, event_data, new_result_str = _intercept_tool_result(tool_name, result_str)
    if event_type is not None and event_data is not None:
        return (event_type, event_data), new_result_str
    return None, new_result_str


def _get_confidence_explanation(score: float) -> str:
    """Return a human-readable explanation for a confidence score."""
    if score >= 4.5:
        return "Very high confidence — used proven patterns and all tools succeeded"
    if score >= 3.5:
        return "High confidence — data looks correct"
    if score >= 2.5:
        return "Moderate confidence — results may need verification"
    return "Low confidence — please verify this data before acting on it"


from app.models.chat import ChatMessage, ChatSession
from app.services.audit_service import log_event
from app.services.chat.billing import deduct_chat_credits
from app.services.chat.llm_adapter import get_adapter
from app.services.chat.nodes import (
    OrchestratorState,
    get_tenant_ai_config,
    retriever_node,
    sanitize_user_input,
)
from app.services.chat.onboarding_tools import (
    ONBOARDING_TOOL_DEFINITIONS,
    execute_onboarding_tool,
)
from app.services.chat.prompts import INPUT_SANITIZATION_PREFIX, ONBOARDING_SYSTEM_PROMPT
from app.services.chat.tool_call_results import build_tool_call_log_entry
from app.services.chat.tools import build_all_tool_definitions, execute_tool_call
from app.services.prompt_template_service import get_active_template

logger = logging.getLogger(__name__)

MAX_STEPS = settings.CHAT_MAX_TOOL_CALLS_PER_TURN  # default 5


def _extract_file_paths(nodes: list[dict]) -> list[str]:
    """Flatten a nested file tree into a sorted list of file paths."""
    paths: list[str] = []
    for node in nodes:
        if not node.get("is_directory"):
            paths.append(node.get("path", ""))
        children = node.get("children")
        if children:
            paths.extend(_extract_file_paths(children))
    return sorted(paths)


def _sanitize_for_prompt(text: str) -> str:
    """Strip control characters and limit length for prompt injection safety."""
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", text)
    return cleaned[:500]


def _is_valid_uuid(val: str) -> bool:
    """Check if a string is a valid UUID."""
    try:
        uuid.UUID(str(val))
        return True
    except (ValueError, AttributeError):
        return False


async def _resolve_default_workspace(
    db: AsyncSession,
    tenant_id: uuid.UUID,
) -> str | None:
    """Find the most recent active workspace for a tenant (cached per request)."""
    from sqlalchemy import select

    from app.models.workspace import Workspace

    result = await db.execute(
        select(Workspace.id)
        .where(Workspace.tenant_id == tenant_id, Workspace.status == "active")
        .order_by(Workspace.created_at.desc())
        .limit(1)
    )
    ws = result.scalar_one_or_none()
    return str(ws) if ws else None


async def _dispatch_memory_update(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
    user_message: str,
    assistant_message: str,
) -> None:
    """Helper to safely run the regex-gated memory updater in the background."""
    from app.services.chat.llm_adapter import get_adapter
    from app.services.chat.memory_updater import maybe_extract_correction

    provider = settings.MULTI_AGENT_SPECIALIST_PROVIDER
    api_key = settings.ANTHROPIC_API_KEY
    model = settings.MULTI_AGENT_SPECIALIST_MODEL

    try:
        adapter = get_adapter(provider, api_key)
        await maybe_extract_correction(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            user_message=user_message,
            assistant_message=assistant_message,
            adapter=adapter,
            model=model,
        )
    except Exception as e:
        logger.error(f"background.memory_update_failed: {e}")


async def _dispatch_content_summary(
    db: AsyncSession,
    message_id: uuid.UUID,
    user_message: str,
    assistant_message: str,
) -> None:
    """Fire-and-forget: generate and persist a factual summary for history."""
    from app.services.chat.summariser import dispatch_content_summary

    try:
        await dispatch_content_summary(
            db=db,
            message_id=message_id,
            user_message=user_message,
            assistant_message=assistant_message,
        )
    except Exception as e:
        logger.error(f"background.content_summary_failed: {e}")


async def run_chat_turn(
    db: AsyncSession,
    session: ChatSession,
    user_message: str,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    user_msg: ChatMessage | None = None,
    wizard_step: str | None = None,
    user_timezone: str | None = None,
) -> AsyncGenerator[dict, None]:
    """Execute an agentic chat turn with Claude's native tool use.

    Signature and return type match the previous linear pipeline —
    chat.py needs zero changes.
    """
    correlation_id = str(uuid.uuid4())

    # ── Load conversation history (summary-based windowing) ──
    from app.services.chat.history_compactor import KEEP_RECENT

    max_turns = settings.CHAT_MAX_HISTORY_TURNS
    all_messages: list[dict] = []
    summarised = 0
    if session.messages:
        msg_list = [m for m in session.messages if m.role in ("user", "assistant")]
        for i, msg in enumerate(msg_list):
            is_recent = i >= len(msg_list) - KEEP_RECENT
            if is_recent or not msg.content_summary:
                all_messages.append({"role": msg.role, "content": msg.content})
            else:
                all_messages.append({"role": msg.role, "content": msg.content_summary})
                summarised += 1

    # Hard cap at max_turns * 2 messages
    history_messages = all_messages[-(max_turns * 2) :]

    # Condense large tool results in older messages to reduce token bloat
    from app.services.chat.history_compactor import build_condensed_history

    original_chars = sum(len(m.get("content", "")) for m in history_messages)
    history_messages = build_condensed_history(history_messages, keep_recent=4)
    condensed_chars = sum(len(m.get("content", "")) for m in history_messages)
    if original_chars != condensed_chars:
        print(f"[ORCHESTRATOR] history condensed: {original_chars} -> {condensed_chars} chars", flush=True)
    print(f"[ORCHESTRATOR] history loaded: {len(history_messages)} messages ({summarised} summarised)", flush=True)

    # ── Save user message (if not already saved by caller) ──
    if user_msg is None:
        user_msg = ChatMessage(
            tenant_id=tenant_id,
            session_id=session.id,
            role="user",
            content=user_message,
        )
        db.add(user_msg)
        await db.flush()

    is_onboarding = getattr(session, "session_type", "chat") == "onboarding"

    # ── Pre-loop: RAG retrieval for doc context (skip for onboarding) ──
    sanitized_input = sanitize_user_input(user_message)
    rag_context = ""
    citations: list[dict] = []

    if not is_onboarding:
        state = OrchestratorState(
            user_message=sanitized_input,
            tenant_id=tenant_id,
            actor_id=user_id,
            session_id=session.id,
            conversation_history=history_messages,
            route={"needs_docs": True},  # always attempt RAG
        )
        try:
            await retriever_node(state, db)
        except Exception:
            logger.warning("Pre-loop RAG retrieval failed, continuing without docs")
            state.doc_chunks = []

        # Build RAG context block
        if state.doc_chunks:
            rag_parts = []
            for chunk in state.doc_chunks:
                rag_parts.append(f"[Documentation: {chunk['title']}]\n{chunk['content']}")
                citations.append(
                    {
                        "type": "doc",
                        "title": chunk["title"],
                        "snippet": chunk["content"][:200],
                    }
                )
            rag_context = "\n\n".join(rag_parts)

    # ── Build messages for Claude ──
    messages: list[dict] = list(history_messages)

    # Compose user message with sanitization prefix and RAG context
    user_content = f"{INPUT_SANITIZATION_PREFIX}\n\n"
    if rag_context:
        user_content += f"<context>\n{rag_context}\n</context>\n\n"
    user_content += f"User question: {sanitized_input}"
    messages.append({"role": "user", "content": user_content})

    # ── Build tool definitions (with policy-based filtering) ──
    connection_warnings: list[str] = []
    if is_onboarding:
        tool_definitions = list(ONBOARDING_TOOL_DEFINITIONS)
    else:
        tool_definitions = await build_all_tool_definitions(db, tenant_id)

        # Pre-flight connection health check — strip tools for dead connections
        connection_warnings = await _check_connection_health(db, tenant_id)
        if connection_warnings:
            tool_definitions = _filter_tools_for_dead_connections(
                tool_definitions, connection_warnings
            )

    # ── Resolve tenant-specific system prompt ──
    if is_onboarding:
        from app.services.chat.prompts import ONBOARDING_STEP_CONTEXTS

        system_prompt = ONBOARDING_SYSTEM_PROMPT
        if wizard_step and wizard_step in ONBOARDING_STEP_CONTEXTS:
            system_prompt = (
                f"{ONBOARDING_SYSTEM_PROMPT}\n\n## Current Step: {wizard_step}\n{ONBOARDING_STEP_CONTEXTS[wizard_step]}"
            )
    else:
        system_prompt = await get_active_template(db, tenant_id)

    # ── Inject brand identity so the AI knows its name ──
    brand_name = ""
    if not is_onboarding:
        from sqlalchemy import select as sa_select

        from app.models.tenant import Tenant, TenantConfig

        config_result = await db.execute(sa_select(TenantConfig.brand_name).where(TenantConfig.tenant_id == tenant_id))
        brand_name = config_result.scalar_one_or_none() or ""
        if not brand_name:
            tenant_result = await db.execute(sa_select(Tenant.name).where(Tenant.id == tenant_id))
            brand_name = tenant_result.scalar_one_or_none() or "Suite Studio AI"
        system_prompt += (
            f'\n\nYour name is "{brand_name}". When asked to introduce yourself '
            f"or asked your name, say you are {brand_name}.\n"
        )

    # ── Inject AI Soul (Tone & Quirks) ──
    soul_bot_tone = ""
    if not is_onboarding:
        from app.services.soul_service import get_soul_config

        soul_config = await get_soul_config(tenant_id)
        if soul_config.exists:
            soul_parts = ["\n\n## Tenant-Specific AI Configuration & Logic\n"]
            if soul_config.bot_tone:
                soul_bot_tone = soul_config.bot_tone
                soul_parts.append(f"TONE & MANNER:\n{soul_config.bot_tone}\n")
            if soul_config.netsuite_quirks:
                soul_parts.append(f"NETSUITE QUIRKS & LOGIC:\n{soul_config.netsuite_quirks}\n")
            system_prompt += "\n".join(soul_parts)

    # ── Inject dynamic tool inventory into system prompt ──
    # This ensures the model always knows the exact tool names it can call,
    # regardless of whether the base prompt matches.
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

    if not is_onboarding and tool_definitions:
        tool_inventory_lines = ["\nAVAILABLE TOOLS (use these exact names when calling tools):"]
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
                    '\n  Parameters: {"reportId": <number>, "dateTo": "YYYY-MM-DD", "dateFrom": "YYYY-MM-DD", "subsidiaryId": <number>}'
                    "\n  → reportId must be a NUMBER (e.g. -200), not a string."
                    "\n  → dateTo is always required. dateFrom is required for P&L, optional for Balance Sheet."
                    "\n  → Call ns_listAllReports FIRST to get reportId and check has_subsidiary_filter / as_of_date_format."
                    "\n  → If has_subsidiary_filter=true, call ns_getSubsidiaries and pass subsidiaryId."
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

        system_prompt += "\n".join(tool_inventory_lines)

    # ── Connection health warning (appended after tool inventory) ──
    if connection_warnings:
        system_prompt += _build_connection_warning_block(connection_warnings)

    # ── Workspace context injection ──
    workspace_context: dict | None = None
    if getattr(session, "workspace_id", None):
        from app.services import workspace_service as ws_svc

        ws = await ws_svc.get_workspace(db, session.workspace_id, tenant_id)
        if ws:
            workspace_context = {
                "workspace_id": str(session.workspace_id),
                "name": ws.name,
            }
            files = await ws_svc.list_files(db, session.workspace_id, tenant_id)
            file_paths = _extract_file_paths(files)
            file_listing = "\n".join(f"- {_sanitize_for_prompt(p)}" for p in file_paths[:50])
            if len(file_paths) > 50:
                file_listing += f"\n... and {len(file_paths) - 50} more files"
            system_prompt += (
                f"\n\nWORKSPACE CONTEXT:\n"
                f"Active workspace: '{ws.name}' (ID: {session.workspace_id}).\n"
                f"Files in workspace:\n{file_listing}\n\n"
                f"Use workspace tools (workspace_list_files, workspace_read_file, "
                f"workspace_search, workspace_propose_patch) to browse and modify files. "
                f"The workspace_id is '{session.workspace_id}' — it will be auto-injected."
            )
            system_prompt += (
                "\nWhen the user mentions they are 'viewing' or 'looking at' a specific file, "
                "or when the message includes '[Currently viewing file: ...]', "
                "use the workspace_read_file tool to read that file's content before responding. "
                "This lets you see exactly what the user sees in their editor."
            )
            system_prompt += (
                "\n\n## IDE Chat Behavior\n"
                "You are an IDE assistant with direct file access. "
                "Be concise — lead with the answer, use code blocks, no preambles.\n"
                "For complex reasoning, use <thinking>...</thinking> tags before your answer. "
                "This block is collapsed by default in the UI.\n"
            )

    # ── Load active policy for tool gating + output redaction ──
    from app.services.policy_service import evaluate_tool_call as policy_evaluate
    from app.services.policy_service import get_active_policy, redact_output

    active_policy = await get_active_policy(db, tenant_id)

    # ── Resolve tenant AI config ──
    provider, model, api_key, is_byok = await get_tenant_ai_config(db, tenant_id)
    adapter = get_adapter(provider, api_key)

    # ── Importance tier default (overridden in unified/legacy paths) ──
    from app.services.importance_classifier import ImportanceTier, classify_importance

    importance_tier = classify_importance(sanitized_input)

    # ── Multi-agent routing (opt-in per tenant or globally) ──
    if not is_onboarding and not workspace_context:
        from sqlalchemy import select as sa_select

        from app.models.tenant import TenantConfig

        use_multi_agent = settings.MULTI_AGENT_ENABLED
        try:
            tc_result = await db.execute(
                sa_select(TenantConfig.multi_agent_enabled).where(TenantConfig.tenant_id == tenant_id)
            )
            tenant_ma = tc_result.scalar_one_or_none()
            if isinstance(tenant_ma, bool):
                use_multi_agent = tenant_ma
        except Exception:
            pass  # Fall through to single-agent

        if use_multi_agent:
            from app.services.chat.llm_adapter import get_adapter as get_specialist_adapter
            from app.services.netsuite_metadata_service import get_active_metadata

            if is_byok:
                # BYOK: use tenant's own provider + API key
                specialist_adapter = get_specialist_adapter(provider, api_key)
            else:
                specialist_adapter = get_specialist_adapter(
                    settings.MULTI_AGENT_SPECIALIST_PROVIDER,
                    settings.ANTHROPIC_API_KEY,
                )
            metadata = await get_active_metadata(db, tenant_id)

            # ── Check for unified agent flag (Phase 2) ──
            use_unified = settings.UNIFIED_AGENT_ENABLED
            try:
                tc_result2 = await db.execute(
                    sa_select(TenantConfig.unified_agent_enabled).where(TenantConfig.tenant_id == tenant_id)
                )
                tenant_ua = tc_result2.scalar_one_or_none()
                if isinstance(tenant_ua, bool):
                    use_unified = tenant_ua
            except Exception:
                logger.warning("unified_agent.flag_check_failed", exc_info=True)

            if use_unified:
                # ── Unified agent path: context assembly → single agent → stream ──
                from app.services.chat.agents import UnifiedAgent

                # ── Chitchat short-circuit: skip expensive context for conversational messages ──
                _is_chitchat = bool(_CHITCHAT_RE.match(sanitized_input))
                is_financial = False

                from app.services.importance_classifier import ImportanceTier, classify_importance

                importance_tier = ImportanceTier.CASUAL  # default for chitchat/MCP

                if _is_chitchat:
                    print("[UNIFIED] Chitchat detected — skipping context assembly", flush=True)
                    context: dict[str, Any] = {"user_timezone": user_timezone}
                    unified_agent = UnifiedAgent(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        correlation_id=correlation_id,
                        metadata=None,
                        policy=active_policy,
                    )
                else:
                    # Detect financial intent for task augmentation + domain knowledge boost
                    from app.services.chat.coordinator import IntentType, classify_intent

                    detected_intent = classify_intent(sanitized_input)
                    is_financial = detected_intent == IntentType.FINANCIAL_REPORT

                    # Gate financial reports by permission
                    if is_financial:
                        from app.core.dependencies import has_permission
                        can_access_financial = await has_permission(db, user_id, "chat.financial_reports")
                        if not can_access_financial:
                            is_financial = False
                            sanitized_input = (
                                sanitized_input
                                + "\n\n[SYSTEM: Financial reports are restricted for your role. "
                                "Respond to the user that financial reports require Admin or User role access. "
                                "Do NOT attempt to call netsuite_financial_report.]"
                            )
                            print("[ORCHESTRATOR] Financial report blocked — user lacks chat.financial_reports permission", flush=True)

                    importance_tier = classify_importance(
                        sanitized_input,
                        intent_hint=detected_intent.value if is_financial else None,
                    )
                    print(
                        f"[ORCHESTRATOR] Importance tier: {importance_tier.label} ({importance_tier.value})",
                        flush=True,
                    )

                    # Follow-up detection: if the previous turn used financial report mode
                    if not is_financial and history_messages:
                        prev_assistant = next(
                            (m["content"] for m in reversed(history_messages) if m["role"] == "assistant"),
                            "",
                        )
                        _financial_history_markers = (
                            "transactionaccountingline",
                            "accttype",
                            "Net Income",
                            "Income Statement",
                            "Balance Sheet",
                            _FINANCIAL_MODE_TAG,
                        )
                        if any(marker in prev_assistant for marker in _financial_history_markers):
                            is_financial = True
                            print("[UNIFIED] Financial follow-up detected from conversation history", flush=True)

                    dk_top_k = 5 if is_financial else None  # default from settings

                    # ── Smart context injection: classify how much context this query needs ──
                    context_need = _classify_context_need(sanitized_input, is_financial=is_financial)
                    print(f"[ORCHESTRATOR] Context need: {context_need}", flush=True)

                    # Injection matrix:
                    #   Block              FULL  DATA  DOCS  WORKSPACE  FINANCIAL
                    #   tenant_schema       ✅    ✅    ❌      ❌        ❌
                    #   table_schemas       ✅    ✅    ❌      ❌        ❌
                    #   tenant_vernacular   ✅    ✅    ❌      ❌        ✅
                    #   domain_knowledge    ✅    ✅    ✅      ❌        ❌
                    #   onboarding_profile  ✅    ❌    ❌      ❌        ✅
                    #   proven_patterns     ✅    ✅    ❌      ❌        ❌

                    _need_vernacular = context_need in (ContextNeed.FULL, ContextNeed.DATA, ContextNeed.FINANCIAL)
                    _need_domain_knowledge = context_need in (ContextNeed.FULL, ContextNeed.DATA, ContextNeed.DOCS)
                    _need_patterns = context_need in (ContextNeed.FULL, ContextNeed.DATA)
                    _need_schemas = context_need in (ContextNeed.FULL, ContextNeed.DATA)
                    _need_onboarding = context_need in (ContextNeed.FULL, ContextNeed.DATA, ContextNeed.FINANCIAL)

                    # Assemble context concurrently (only fetch what we need)
                    from app.services.chat.domain_knowledge import retrieve_domain_knowledge
                    from app.services.chat.tenant_resolver import TenantEntityResolver
                    from app.services.query_pattern_service import retrieve_similar_patterns

                    # Build the list of concurrent tasks dynamically
                    _gather_tasks = []
                    _gather_keys = []

                    if _need_vernacular:
                        _gather_tasks.append(
                            TenantEntityResolver.resolve_entities(
                                user_message=sanitized_input,
                                tenant_id=tenant_id,
                                db=db,
                                adapter=specialist_adapter,
                                model=settings.MULTI_AGENT_SPECIALIST_MODEL,
                            )
                        )
                        _gather_keys.append("vernacular")
                    if _need_domain_knowledge:
                        _gather_tasks.append(
                            retrieve_domain_knowledge(db=db, query_text=sanitized_input, top_k=dk_top_k)
                        )
                        _gather_keys.append("dk")
                    if _need_patterns:
                        _gather_tasks.append(
                            retrieve_similar_patterns(db, tenant_id, sanitized_input)
                        )
                        _gather_keys.append("patterns")

                    _gather_results = await asyncio.gather(*_gather_tasks, return_exceptions=True)
                    _results = dict(zip(_gather_keys, _gather_results))

                    vernacular_result = _results.get("vernacular")
                    dk_result = _results.get("dk")
                    patterns_result = _results.get("patterns")

                    context: dict[str, Any] = {}

                    # tenant_vernacular (FULL, DATA, FINANCIAL)
                    if vernacular_result is not None:
                        if isinstance(vernacular_result, Exception):
                            logger.warning("unified_agent.entity_resolution_failed", exc_info=vernacular_result)
                        elif vernacular_result:
                            context["tenant_vernacular"] = vernacular_result
                            print(f"[UNIFIED] Vernacular injected ({len(vernacular_result)} chars)", flush=True)
                            import re as _re
                            conf_scores = [
                                float(s)
                                for s in _re.findall(
                                    r"<confidence_score>([\d.]+)</confidence_score>",
                                    vernacular_result,
                                )
                            ]
                            if conf_scores:
                                context["entity_resolution_confidence"] = max(conf_scores)

                    # domain_knowledge (FULL, DATA, DOCS — skip for FINANCIAL & WORKSPACE)
                    if dk_result is not None:
                        if is_financial:
                            context["domain_knowledge"] = []
                        elif isinstance(dk_result, Exception):
                            logger.warning("unified_agent.domain_knowledge_failed", exc_info=dk_result)
                        elif dk_result:
                            context["domain_knowledge"] = [r["raw_text"] for r in dk_result]
                            print(f"[UNIFIED] Domain knowledge injected ({len(dk_result)} chunks)", flush=True)
                            sims = [r["similarity"] for r in dk_result if r.get("similarity")]
                            if sims:
                                context["domain_knowledge_similarity"] = sum(sims) / len(sims)

                    # proven_patterns (FULL, DATA — skip for DOCS, WORKSPACE, FINANCIAL)
                    if patterns_result is not None:
                        if isinstance(patterns_result, Exception):
                            logger.warning("unified_agent.proven_patterns_failed", exc_info=patterns_result)
                        elif patterns_result:
                            context["proven_patterns"] = patterns_result
                            print(f"[UNIFIED] Proven patterns injected ({len(patterns_result)} patterns)", flush=True)
                            context["matched_pattern_similarity"] = patterns_result[0].get("similarity", 0.0)
                            context["matched_pattern_success_count"] = patterns_result[0].get("success_count", 0)

                    context["user_timezone"] = user_timezone
                    context["importance_tier"] = importance_tier.value

                    # table_schemas (FULL, DATA — skip for DOCS, WORKSPACE, FINANCIAL)
                    if not _need_schemas:
                        print(f"[ORCHESTRATOR] Skipping schema injection for {context_need} query", flush=True)
                    else:
                        try:
                            from app.services.schema_context_selector import select_relevant_schemas
                            from app.services.prompt_template_service import _build_table_schema_section

                            entity_types: list[str] = []
                            if isinstance(vernacular_result, str):
                                import re as _re_schema
                                entity_types = _re_schema.findall(
                                    r"<entity_type>(.*?)</entity_type>", vernacular_result
                                )

                            relevant_tables = select_relevant_schemas(
                                sanitized_input,
                                entity_types=entity_types,
                            )
                            print(f"[ORCHESTRATOR] Schema tables selected: {relevant_tables}", flush=True)

                            schema_xml = _build_table_schema_section(
                                metadata=metadata,
                                relevant_tables=relevant_tables,
                            )
                            if schema_xml:
                                context["table_schemas"] = schema_xml
                                print(
                                    f"[ORCHESTRATOR] Schema injected ({len(schema_xml)} chars, "
                                    f"{len(relevant_tables)} tables)",
                                    flush=True,
                                )
                        except Exception:
                            logger.warning("orchestrator.schema_injection_failed", exc_info=True)

                    # onboarding_profile (FULL, FINANCIAL — skip for DATA, DOCS, WORKSPACE)
                    if not _need_onboarding:
                        print(f"[ORCHESTRATOR] Skipping onboarding profile for {context_need} query", flush=True)
                    else:
                        try:
                            from app.models.tenant import TenantConfig as _TC

                            _op_result = await db.execute(
                                sa_select(_TC.onboarding_profile).where(_TC.tenant_id == tenant_id)
                            )
                            _onboarding_profile = _op_result.scalar_one_or_none()
                            if _onboarding_profile and isinstance(_onboarding_profile, dict):
                                profile_parts: list[str] = []

                                # Transaction landscape
                                txn_types = _onboarding_profile.get("transaction_types", [])
                                if txn_types:
                                    txn_summary = "\n".join(
                                        f"  - {t['type']}: {t['count']} records ({t.get('earliest', '?')} to {t.get('latest', '?')})"
                                        for t in txn_types[:15]
                                    )
                                    profile_parts.append(
                                        f"<tenant_transaction_landscape>\nThis tenant uses these transaction types:\n{txn_summary}\n</tenant_transaction_landscape>"
                                    )

                                # Relationship map
                                rels = _onboarding_profile.get("transaction_relationships", [])
                                if rels:
                                    rel_summary = "\n".join(
                                        f"  - {r['source']} -> {r['target']} ({r['count']} links)"
                                        for r in rels[:20]
                                    )
                                    profile_parts.append(
                                        f"<transaction_relationships>\n{rel_summary}\n</transaction_relationships>"
                                    )

                                # Status codes
                                status_map = _onboarding_profile.get("status_codes", {})
                                if status_map:
                                    status_lines: list[str] = []
                                    for _txn_type, codes in list(status_map.items())[:10]:
                                        for c in codes[:8]:
                                            status_lines.append(
                                                f"  - {_txn_type} status '{c['code']}' = {c['display']} ({c['count']})"
                                            )
                                    if status_lines:
                                        profile_parts.append(
                                            "<tenant_status_codes>\n" + "\n".join(status_lines) + "\n</tenant_status_codes>"
                                        )

                                if profile_parts:
                                    profile_xml = "\n".join(profile_parts)
                                    context["onboarding_profile"] = profile_xml
                                    print(f"[ORCHESTRATOR] Onboarding profile injected ({len(profile_xml)} chars)", flush=True)
                        except Exception:
                            logger.warning("orchestrator.onboarding_profile_injection_failed", exc_info=True)

                    # Create unified agent — pass metadata only when schemas are needed
                    unified_agent = UnifiedAgent(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        correlation_id=correlation_id,
                        metadata=metadata if _need_schemas else None,
                        policy=active_policy,
                    )

                # Augment task for financial report queries
                unified_task = sanitized_input
                if not _is_chitchat and is_financial:
                    unified_task = _build_financial_mode_task(sanitized_input)
                    print("[UNIFIED] Financial report mode activated (SuiteQL + CONSOLIDATE)", flush=True)

                streamed_text_parts: list[str] = []
                agent_result = None
                last_structured_output: dict | None = None

                unified_model = model if is_byok else settings.MULTI_AGENT_SQL_MODEL

                # Route simple lookups to Haiku for 10x speed + cost savings
                # Only for non-BYOK tenants (BYOK users chose their model)
                if (
                    not is_byok
                    and not _is_chitchat
                    and not is_financial
                    and importance_tier.value <= 2
                    and _is_simple_lookup(sanitized_input)
                ):
                    unified_model = HAIKU_MODEL
                    print(f"[ORCHESTRATOR] Simple lookup detected — routing to Haiku", flush=True)

                async for event_type, payload in unified_agent.run_streaming(
                    task=unified_task,
                    context=context,
                    db=db,
                    adapter=specialist_adapter,
                    model=unified_model,
                    conversation_history=history_messages,
                    tool_choice=None,
                    tool_result_interceptor=_tool_interceptor,
                ):
                    if event_type == "text":
                        streamed_text_parts.append(payload)
                        yield {"type": "text", "content": payload}
                    elif event_type == "tool_status":
                        yield {"type": "tool_status", "content": payload}
                    elif event_type == "tool_intercept":
                        # payload is (event_type_str, event_data_dict)
                        last_structured_output = {"type": payload[0], "data": payload[1]}
                        yield {"type": payload[0], "data": payload[1]}
                    elif event_type == "response":
                        agent_result = payload

                if agent_result is None:
                    final_text = (
                        _sanitize_assistant_text("".join(streamed_text_parts))
                        or "I wasn't able to process that request."
                    )
                    coord_result_tokens = (0, 0)
                    coord_result_cache = (0, 0)
                    coord_result_tool_calls: list[dict] = []
                else:
                    final_text = _sanitize_assistant_text(agent_result.data or "")
                    coord_result_tokens = (
                        agent_result.tokens_used.input_tokens,
                        agent_result.tokens_used.output_tokens,
                    )
                    coord_result_cache = (
                        agent_result.tokens_used.cache_creation_input_tokens,
                        agent_result.tokens_used.cache_read_input_tokens,
                    )
                    coord_result_tool_calls = agent_result.tool_calls_log

                # Extract confidence score from agent result
                confidence_val = (
                    agent_result.confidence_score
                    if agent_result and agent_result.confidence_score is not None
                    else None
                )
                if confidence_val is not None:
                    yield {
                        "type": "confidence",
                        "score": confidence_val,
                        "explanation": _get_confidence_explanation(confidence_val),
                    }

                # Emit importance tier SSE event
                yield {
                    "type": "importance",
                    "tier": importance_tier.value,
                    "label": importance_tier.label,
                    "needs_review": (
                        agent_result is not None
                        and any(
                            isinstance(tc.get("result"), dict)
                            and tc.get("result", {}).get("judge_verdict", {}).get("needs_review")
                            for tc in (agent_result.tool_calls_log or [])
                        )
                    ),
                }

                assistant_msg = ChatMessage(
                    tenant_id=tenant_id,
                    session_id=session.id,
                    role="assistant",
                    content=final_text or "I wasn't able to find relevant information for that question.",
                    tool_calls=coord_result_tool_calls if coord_result_tool_calls else None,
                    citations=citations if citations else None,
                    token_count=coord_result_tokens[0] + coord_result_tokens[1],
                    input_tokens=coord_result_tokens[0],
                    output_tokens=coord_result_tokens[1],
                    model_used=unified_model,
                    provider_used=provider if is_byok else settings.MULTI_AGENT_SPECIALIST_PROVIDER,
                    is_byok=is_byok,
                    confidence_score=confidence_val,
                    query_importance=importance_tier.value,
                    structured_output=last_structured_output,
                    created_at=datetime.now(timezone.utc),
                )
                db.add(assistant_msg)

                if not session.title:
                    session.title = user_message[:100].strip()
                session.updated_at = func.now()

                audit_payload: dict[str, Any] = {
                    "mode": "unified_agent",
                    "provider": settings.MULTI_AGENT_SPECIALIST_PROVIDER,
                    "model": settings.MULTI_AGENT_SQL_MODEL,
                    "steps": len(coord_result_tool_calls),
                    "input_tokens": coord_result_tokens[0],
                    "output_tokens": coord_result_tokens[1],
                    "cache_creation_tokens": coord_result_cache[0],
                    "cache_read_tokens": coord_result_cache[1],
                    "doc_chunks_count": len(state.doc_chunks) if state.doc_chunks else 0,
                    "tools_called": [t["tool"] for t in coord_result_tool_calls],
                }
                if coord_result_cache[1] > 0:
                    # Log cache savings for visibility
                    print(f"[ORCHESTRATOR] Cache hit: {coord_result_cache[1]:,} tokens read from cache", flush=True)
                await log_event(
                    db=db,
                    tenant_id=tenant_id,
                    category="chat",
                    action="chat.turn",
                    actor_id=user_id,
                    resource_type="chat_session",
                    resource_id=str(session.id),
                    correlation_id=correlation_id,
                    payload=audit_payload,
                )

                if not is_byok:
                    await deduct_chat_credits(db, tenant_id, settings.MULTI_AGENT_SQL_MODEL)

                await db.commit()

                asyncio.create_task(
                    _dispatch_memory_update(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        db=db,
                        user_message=sanitized_input,
                        assistant_message=final_text,
                    )
                )
                asyncio.create_task(
                    _dispatch_content_summary(
                        db=db,
                        message_id=assistant_msg.id,
                        user_message=sanitized_input,
                        assistant_message=final_text,
                    )
                )

                result_msg = {
                    "id": str(assistant_msg.id),
                    "role": assistant_msg.role,
                    "content": assistant_msg.content,
                    "tool_calls": assistant_msg.tool_calls,
                    "citations": assistant_msg.citations,
                }
                if hasattr(assistant_msg, "created_at") and assistant_msg.created_at:
                    result_msg["created_at"] = assistant_msg.created_at.isoformat()
                if confidence_val is not None:
                    result_msg["confidence_score"] = confidence_val
                result_msg["query_importance"] = importance_tier.value
                yield {"type": "message", "message": result_msg}
                return

            # ── Legacy multi-agent path ──
            from app.services.importance_classifier import ImportanceTier, classify_importance
            from app.services.chat.coordinator import MultiAgentCoordinator

            importance_tier = classify_importance(sanitized_input)

            coordinator = MultiAgentCoordinator(
                db=db,
                tenant_id=tenant_id,
                user_id=user_id,
                correlation_id=correlation_id,
                main_adapter=adapter,
                main_model=model,
                specialist_adapter=specialist_adapter,
                specialist_model=settings.MULTI_AGENT_SPECIALIST_MODEL,
                metadata=metadata,
                policy=active_policy,
                system_prompt=system_prompt,
                user_timezone=user_timezone,
            )
            coordinator.soul_tone = soul_bot_tone
            coordinator.brand_name = brand_name

            # Stream multi-agent: dispatch agents first, then stream synthesis
            streamed_text_parts: list[str] = []
            coord_structured_output: dict | None = None
            async for event in coordinator.run_streaming(
                user_message=sanitized_input,
                conversation_history=history_messages,
                rag_context=rag_context,
            ):
                if event["type"] == "text":
                    streamed_text_parts.append(event["content"])
                if event["type"] in ("data_table", "financial_report"):
                    coord_structured_output = {"type": event["type"], "data": event["data"]}
                yield event
                if event["type"] == "message":
                    # Final message already yielded — save and return
                    break

            coord_result = coordinator.last_result
            if coord_result is None:
                # Fallback: synthesis didn't produce a result
                final_text = (
                    _sanitize_assistant_text("".join(streamed_text_parts))
                    or "I wasn't able to find relevant information for that question. Could you rephrase or provide more details?"
                )
                coord_result_tokens = (0, 0)
                coord_result_tool_calls: list[dict] = []
            else:
                final_text = _sanitize_assistant_text(coord_result.final_text)
                coord_result_tokens = (coord_result.total_input_tokens, coord_result.total_output_tokens)
                coord_result_tool_calls = coord_result.tool_calls_log

            assistant_msg = ChatMessage(
                tenant_id=tenant_id,
                session_id=session.id,
                role="assistant",
                content=final_text
                or "I wasn't able to find relevant information for that question. Could you rephrase or provide more details?",
                tool_calls=coord_result_tool_calls if coord_result_tool_calls else None,
                citations=citations if citations else None,
                token_count=coord_result_tokens[0] + coord_result_tokens[1],
                input_tokens=coord_result_tokens[0],
                output_tokens=coord_result_tokens[1],
                model_used=model,
                provider_used=provider,
                is_byok=is_byok,
                query_importance=importance_tier.value,
                structured_output=coord_structured_output,
                created_at=datetime.now(timezone.utc),
            )
            db.add(assistant_msg)

            if not session.title:
                session.title = user_message[:100].strip()
            # Always bump updated_at so session re-sorts to top of list
            session.updated_at = func.now()

            audit_payload: dict[str, Any] = {
                "mode": "multi_agent",
                "provider": provider,
                "model": model,
                "specialist_model": settings.MULTI_AGENT_SPECIALIST_MODEL,
                "steps": len(coord_result_tool_calls),
                "input_tokens": coord_result_tokens[0],
                "output_tokens": coord_result_tokens[1],
                "doc_chunks_count": len(state.doc_chunks) if state.doc_chunks else 0,
                "tools_called": [t["tool"] for t in coord_result_tool_calls],
            }
            await log_event(
                db=db,
                tenant_id=tenant_id,
                category="chat",
                action="chat.turn",
                actor_id=user_id,
                resource_type="chat_session",
                resource_id=str(session.id),
                correlation_id=correlation_id,
                payload=audit_payload,
            )

            # Tollbooth: deduct credits before commit (skip for BYOK users)
            if not is_byok:
                await deduct_chat_credits(db, tenant_id, model)

            await db.commit()

            # Fire-and-forget background tasks
            asyncio.create_task(
                _dispatch_memory_update(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    db=db,
                    user_message=sanitized_input,
                    assistant_message=final_text,
                )
            )
            asyncio.create_task(
                _dispatch_content_summary(
                    db=db,
                    message_id=assistant_msg.id,
                    user_message=sanitized_input,
                    assistant_message=final_text,
                )
            )

            # If we already yielded the final message via streaming, just return
            # Otherwise yield a final message now
            if not streamed_text_parts:
                result_msg = {
                    "id": str(assistant_msg.id),
                    "role": assistant_msg.role,
                    "content": assistant_msg.content,
                    "tool_calls": assistant_msg.tool_calls,
                    "citations": assistant_msg.citations,
                }
                if hasattr(assistant_msg, "created_at") and assistant_msg.created_at:
                    result_msg["created_at"] = assistant_msg.created_at.isoformat()
                result_msg["query_importance"] = importance_tier.value
                yield {"type": "message", "message": result_msg}
            return

    # ── Split system prompt for Anthropic prompt caching ──
    prompt_parts = split_system_prompt(system_prompt)

    # ── Single-agent agentic loop (default path) ──
    tool_calls_log: list[dict] = []
    final_text = ""
    total_input_tokens = 0
    total_output_tokens = 0
    last_structured_output: dict | None = None

    for step in range(MAX_STEPS):
        response = None

        # Determine if we should stream using stream_message or fallback
        # (Assuming all adapters implemented stream_message, else this would fail)
        async for event_type, payload in adapter.stream_message(
            model=model,
            max_tokens=16384,
            system=prompt_parts.static,
            system_dynamic=prompt_parts.dynamic,
            messages=messages,
            tools=tool_definitions if tool_definitions else None,
        ):
            if event_type == "text":
                yield {"type": "text", "content": payload}
            elif event_type == "response":
                response = payload

        if not response:
            break

        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens

        if not response.tool_use_blocks:
            # Pure text response — we're done
            final_text = "\n".join(response.text_blocks) if response.text_blocks else ""
            break

        # Append assistant message
        messages.append(adapter.build_assistant_message(response))

        # Execute each tool call and collect results
        tool_results_content = []
        for block in response.tool_use_blocks:
            # Auto-inject workspace_id for workspace tools
            if block.name.startswith("workspace_"):
                if "workspace_id" not in block.input or not _is_valid_uuid(block.input.get("workspace_id", "")):
                    if workspace_context:
                        block.input["workspace_id"] = workspace_context["workspace_id"]
                    elif not workspace_context:
                        # Auto-resolve default workspace for non-workspace sessions
                        resolved_ws_id = await _resolve_default_workspace(db, tenant_id)
                        if resolved_ws_id:
                            block.input["workspace_id"] = resolved_ws_id
                            if not workspace_context:
                                workspace_context = {"workspace_id": resolved_ws_id, "name": "auto-resolved"}

            yield {"type": "tool_status", "content": f"Executing {block.name}..."}

            # Route tool execution: onboarding tools vs standard tools
            t0 = time.monotonic()
            if is_onboarding and block.name in (
                "save_onboarding_profile",
                "start_netsuite_oauth",
                "check_netsuite_connection",
            ):
                result_str = await execute_onboarding_tool(
                    tool_name=block.name,
                    tool_input=block.input,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    db=db,
                )
            else:
                # Policy evaluation: check if tool call is allowed
                policy_result = policy_evaluate(active_policy, block.name, block.input)
                if not policy_result["allowed"]:
                    result_str = json.dumps({"error": f"Policy blocked: {policy_result.get('reason', 'Not allowed')}"})
                else:
                    result_str = await execute_tool_call(
                        tool_name=block.name,
                        tool_input=block.input,
                        tenant_id=tenant_id,
                        actor_id=user_id,
                        correlation_id=correlation_id,
                        db=db,
                    )

                    # Output redaction: strip blocked fields from tool results
                    if active_policy and active_policy.blocked_fields:
                        try:
                            parsed = json.loads(result_str)
                            parsed = redact_output(active_policy, parsed)
                            result_str = json.dumps(parsed, default=str)
                        except (json.JSONDecodeError, TypeError):
                            pass  # Non-JSON result, skip redaction

            # Intercept data tool results: emit SSE event with full data,
            # condense result_str to summary-only for the LLM.
            intercept_type, intercept_data, result_str = _intercept_tool_result(block.name, result_str)
            if intercept_type is not None:
                last_structured_output = {"type": intercept_type, "data": intercept_data}
                yield {"type": intercept_type, "data": intercept_data}

            tool_results_content.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                }
            )

            # Log for audit
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            tool_calls_log.append(
                build_tool_call_log_entry(
                    step=step,
                    tool_name=block.name,
                    params=block.input,
                    result_str=result_str,
                    duration_ms=elapsed_ms,
                )
            )

        messages.append(adapter.build_tool_result_message(tool_results_content))

    else:
        # Loop exhausted — make one final call without tools to force text
        logger.warning(
            "Agentic loop exhausted %d steps, forcing final response",
            MAX_STEPS,
        )
        response = None
        async for event_type, payload in adapter.stream_message(
            model=model,
            max_tokens=8192,
            system=prompt_parts.static,
            system_dynamic=prompt_parts.dynamic,
            messages=messages,
        ):
            if event_type == "text":
                yield {"type": "text", "content": payload}
            elif event_type == "response":
                response = payload

        if response:
            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens
            final_text = "\n".join(response.text_blocks) if response.text_blocks else ""

    # Strip raw tool reference tags / leaked XML the LLM may include
    final_text = _sanitize_assistant_text(final_text)

    # ── Save assistant message ──
    assistant_msg = ChatMessage(
        tenant_id=tenant_id,
        session_id=session.id,
        role="assistant",
        content=final_text
        or "I wasn't able to find relevant information for that question. Could you rephrase or provide more details?",
        tool_calls=tool_calls_log if tool_calls_log else None,
        citations=citations if citations else None,
        token_count=total_input_tokens + total_output_tokens,
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        model_used=model,
        provider_used=provider,
        is_byok=is_byok,
        query_importance=importance_tier.value,
        structured_output=last_structured_output,
        created_at=datetime.now(timezone.utc),
    )
    db.add(assistant_msg)

    # Auto-title from first message
    if not session.title:
        session.title = user_message[:100].strip()
    # Always bump updated_at so session re-sorts to top of list
    session.updated_at = func.now()

    # Audit
    audit_payload: dict[str, Any] = {
        "mode": "agentic",
        "provider": provider,
        "model": model,
        "steps": len(tool_calls_log),
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "doc_chunks_count": 0 if is_onboarding else (len(state.doc_chunks) if state.doc_chunks else 0),
        "tools_called": [t["tool"] for t in tool_calls_log],
    }
    if workspace_context:
        audit_payload["workspace_id"] = workspace_context["workspace_id"]
    await log_event(
        db=db,
        tenant_id=tenant_id,
        category="chat",
        action="chat.turn",
        actor_id=user_id,
        resource_type="chat_session",
        resource_id=str(session.id),
        correlation_id=correlation_id,
        payload=audit_payload,
    )

    # Tollbooth: deduct credits before commit (skip for BYOK users)
    if not is_byok:
        await deduct_chat_credits(db, tenant_id, model)

    await db.commit()

    # Fire-and-forget background tasks
    asyncio.create_task(
        _dispatch_memory_update(
            tenant_id=tenant_id,
            user_id=user_id,
            db=db,
            user_message=sanitized_input,
            assistant_message=final_text,
        )
    )
    asyncio.create_task(
        _dispatch_content_summary(
            db=db,
            message_id=assistant_msg.id,
            user_message=sanitized_input,
            assistant_message=final_text,
        )
    )

    result_msg = {
        "id": str(assistant_msg.id),
        "role": assistant_msg.role,
        "content": assistant_msg.content,
        "tool_calls": assistant_msg.tool_calls,
        "citations": assistant_msg.citations,
    }
    if hasattr(assistant_msg, "created_at") and assistant_msg.created_at:
        result_msg["created_at"] = assistant_msg.created_at.isoformat()
    result_msg["query_importance"] = importance_tier.value

    yield {"type": "message", "message": result_msg}
