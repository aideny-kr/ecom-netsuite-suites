"""Base specialist agent for the multi-agent orchestration system.

Each specialist runs its own mini agentic loop (up to max_steps iterations)
using the same LLM adapter and tool execution infrastructure as the main
orchestrator, but scoped to a specific task and tool subset.
"""

from __future__ import annotations

import abc
import inspect
import json
import logging
import re
import time
import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable
from xml.sax.saxutils import escape as _xml_escape

from app.services.chat import thinking
from app.services.chat.llm_adapter import BaseLLMAdapter, LLMResponse, TokenUsage
from app.services.chat.prompt_cache import split_system_prompt
from app.services.chat.tool_call_results import (
    build_tool_call_log_entry,
    tool_call_had_error,
    tool_call_row_count,
)
from app.services.chat.tool_categories import categorize
from app.services.chat.write_confirmation_service import build_confirmation_payload
from app.services.chat.write_payload import PayloadParseError
from app.services.chat.write_validation import validate_mutation
from app.services.chat.write_validator import ValidationResult


class WriteRepairState:
    """Bounded repair budget for write validation, held in run state.

    A cap the model is asked to respect is a request; a counter that persists
    and decrements is a guarantee. Exits carry a reason, never a bare boolean.

    Everything here is keyed by `record_type`, never by tool-call id. Stall
    detection needs the failure fingerprint to persist ACROSS separate tool
    calls within one repair cycle: the model reproposes a rejected write as a
    brand-new tool_use block (a new `block.id`) after each rejection, so
    keying on call id would make every repair attempt look like a fresh
    write and stall detection would never fire.

    Keying by `record_type` alone has its own failure mode: a turn that
    creates one journalEntry (exhausting its repair budget) and later
    proposes a second, unrelated journalEntry of the same type must not
    inherit the first write's exhausted attempts. `should_repair` resets a
    record_type's attempt count and fingerprint the moment its repair cycle
    ends (done/stall/budget) — see `_finish_cycle` — so the NEXT logical
    write of that type starts with a full budget. That reset only ever runs
    AFTER a cycle has already been decided, so it cannot weaken stall
    detection within a still-open cycle: the fingerprint comparison that
    detects a stall runs earlier in the same call, against the fingerprint
    left by the PREVIOUS call in that same cycle, before any reset happens.

    Exit reasons are ALSO keyed by record_type (`exit_reason_for`) rather
    than living on one instance-wide field — a turn that repairs record_type
    A and then touches record_type B must never let A's exit reason read
    through as B's.
    """

    def __init__(self, max_attempts: int = 2) -> None:
        self.max_attempts = max_attempts
        self._attempts: dict[str, int] = {}
        self._fingerprints: dict[str, str] = {}
        self._exit_reasons: dict[str, str | None] = {}

    def should_repair(self, record_type: str, result: ValidationResult) -> bool:
        if result.ok:
            self._finish_cycle(record_type, "done")
            return False

        fingerprint = result.fingerprint()
        if self._fingerprints.get(record_type) == fingerprint:
            # Same failure as last time — recomposing will not help.
            self._finish_cycle(record_type, "stall")
            return False

        attempts = self._attempts.get(record_type, 0)
        if attempts >= self.max_attempts:
            self._finish_cycle(record_type, "budget")
            return False

        self._attempts[record_type] = attempts + 1
        self._fingerprints[record_type] = fingerprint
        return True

    def _finish_cycle(self, record_type: str, reason: str) -> None:
        """Record why `record_type`'s repair cycle ended, then clear its
        attempt count and fingerprint so a later, distinct write of the same
        type starts fresh instead of inheriting this cycle's exhausted
        budget (the reported bug)."""
        self._exit_reasons[record_type] = reason
        self._attempts.pop(record_type, None)
        self._fingerprints.pop(record_type, None)

    def exit_reason_for(self, record_type: str) -> str | None:
        """Why `record_type`'s most recently DECIDED repair cycle ended.

        `None` covers both "never seen" and "mid-cycle, still repairing".
        Scoped per record_type so a caller checking record type B's outcome
        never reads record type A's exit reason."""
        return self._exit_reasons.get(record_type)


def _validation_failure_detail(validation: ValidationResult) -> str:
    """Human-readable summary of what a ``ValidationResult`` got wrong.

    Shared by the repair loop's "requesting another attempt" log entry and
    the confirmation-card log entry for a card shown despite an unresolved
    failure (repair exhausted) — both need to name which categories failed,
    not just that something did.
    """
    bits = [
        f"missing required: {', '.join(validation.missing_required)}" if validation.missing_required else None,
        f"missing line fields: {', '.join(validation.missing_line_required)}"
        if validation.missing_line_required
        else None,
        f"invariant violations: {'; '.join(validation.invariant_errors)}" if validation.invariant_errors else None,
    ]
    return "; ".join(b for b in bits if b) or "validation failed"


def _metadata_fetched_this_turn(tool_calls_log: list[dict[str, Any]], record_type: str) -> bool:
    """True if `tool_calls_log` already contains a prior `ns_getRecordTypeMetadata`
    call for *record_type* earlier in this turn.

    Backs the investigation gate (agentic-repair design requirement A): a
    create/upsert proposal reaching the mutation intercept with no prior
    same-turn metadata lookup for its own record type is bounced back to the
    model rather than validated. Matches on `record_type` only — a metadata
    call logged for a DIFFERENT record type does not satisfy this one's gate.
    """
    from app.services.chat.tools import parse_external_tool_name

    for entry in tool_calls_log:
        parsed = parse_external_tool_name(entry.get("tool", ""))
        if not parsed:
            continue
        _, raw_name = parsed
        if raw_name != "ns_getRecordTypeMetadata":
            continue
        params = entry.get("params") or {}
        if params.get("recordType") == record_type:
            return True
    return False


def _build_learned_rules_block(learned_rules: list) -> str:
    """Render the tenant <learned_rules> block, XML-escaping each rule so admin
    rule text containing markup can't break out of the block or inject prompt
    instructions. Returns "" when there are no rules."""
    if not learned_rules:
        return ""
    block = "\n<learned_rules>\nTenant-specific business rules — FOLLOW THESE STRICTLY:\n"
    for rule in learned_rules:
        block += f"- {_xml_escape(str(rule))}\n"
    block += "</learned_rules>"
    return block


from app.services.confidence_extractor import extract_structured_confidence
from app.services.confidence_service import CompositeScorer

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _is_valid_uuid(val: str) -> bool:
    """Check if a string is a valid UUID."""
    import uuid as _uuid

    try:
        _uuid.UUID(str(val))
        return True
    except (ValueError, AttributeError):
        return False


_MAX_ERROR_CHARS = 1000
_MAX_RESULT_ROWS = 500  # Cap rows sent back to LLM (50 was too low for grouped queries like 14 platforms x 10 weeks)


def build_current_date_block(user_timezone: str | None) -> str:
    """Build a "## CURRENT DATE & TIME" system-prompt block.

    Uses the user's timezone when available; falls back to UTC. Unconditional —
    every agent (unified or specialized) should call this so the LLM never has
    to guess from its training cutoff for queries like "last 4 months",
    "this quarter", "yesterday", etc.

    Returns a multi-line string with a header and date context, or an empty
    string on unexpected failure (so callers can safely concat the result).
    """
    from datetime import datetime, timedelta
    from datetime import timezone as _tz

    try:
        tz_label = "UTC"
        local_now = datetime.now(_tz.utc)
        if user_timezone:
            try:
                from zoneinfo import ZoneInfo

                local_now = datetime.now(ZoneInfo(user_timezone))
                tz_label = user_timezone
            except Exception:
                # Unknown timezone name — fall through to UTC
                pass

        local_today = local_now.strftime("%Y-%m-%d")
        local_yesterday = (local_now - timedelta(days=1)).strftime("%Y-%m-%d")
        # Wrapped in <current_datetime> tags so split_system_prompt routes the
        # whole block to the per-turn ``dynamic`` system block. Without this
        # wrapper, HH:MM lives in the cached static prefix and busts the
        # prompt cache every minute.
        return (
            "\n<current_datetime>\n"
            "## CURRENT DATE & TIME\n"
            f"Timezone: {tz_label}. "
            f"Today: {local_today} ({local_now.strftime('%A, %B %d, %Y')}), "
            f"local time: {local_now.strftime('%H:%M')}. "
            f"'today' = TO_DATE('{local_today}', 'YYYY-MM-DD'). "
            f"'yesterday' = TO_DATE('{local_yesterday}', 'YYYY-MM-DD'). "
            f"When the user says 'last N months', anchor on the month BEFORE "
            f"today's month as the most recent complete month."
            "\n</current_datetime>"
        )
    except Exception:
        # Date injection must NEVER break a turn
        return ""


# Pattern to detect data queries that MUST be executed, not answered from memory
_QUERY_PATTERN = re.compile(r"\bSELECT\b", re.IGNORECASE)
_DATA_QUESTION_KEYWORDS = {
    "how many",
    "total",
    "count",
    "sum",
    "average",
    "quantity",
    "revenue",
    "sales",
    "orders",
    "inventory",
}


def _task_contains_query(task: str) -> bool:
    """Check if the task contains a SQL query or data question that requires tool execution."""
    if _QUERY_PATTERN.search(task):
        return True
    task_lower = task.lower()
    return any(kw in task_lower for kw in _DATA_QUESTION_KEYWORDS)


_MIN_ENTITY_CONFIDENCE = 0.70  # Minimum pg_trgm similarity for entity resolver matches

# Tools that should never be skipped by early exit (knowledge/context, not data)
_KNOWLEDGE_TOOLS = frozenset(
    {
        "workspace_search",
        "workspace_read_file",
        "workspace_list_files",
        "rag_search",
        "web_search",
    }
)


def _has_successful_data_result(result_strings: list[str]) -> bool:
    """Check if any tool result string contains successful data rows.

    Checks three formats: local SuiteQL (rows), external MCP (data), financial (items).
    Returns False on errors or empty results so we don't nudge prematurely.
    """
    for result_str in result_strings:
        try:
            parsed = json.loads(result_str)
            if not isinstance(parsed, dict):
                continue
            # Skip errors
            if parsed.get("error"):
                continue
            # Local SuiteQL format
            if isinstance(parsed.get("rows"), list) and len(parsed["rows"]) > 0:
                return True
            # External MCP format
            if isinstance(parsed.get("data"), list) and len(parsed["data"]) > 0:
                return True
            # Financial report format
            if isinstance(parsed.get("items"), list) and len(parsed["items"]) > 0:
                return True
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
    return False


_DATA_SUCCESS_NUDGE = (
    "\n\n⚠️ SYSTEM: A query returned data successfully. "
    "You SHOULD present these results to the user now. "
    "Do NOT run additional queries unless the data is clearly wrong "
    "or missing what the user asked for."
)


def _suppress_metric_value_for_llm(result_str: str) -> str:
    """Withhold a metric's computed number from the LLM-facing tool_result string.

    The metric trust boundary (render the number on the FE, never let the LLM
    state/recompute it) is wired into the STREAMING interceptor via
    ``_intercept_tool_result``. The non-streaming ``run()`` path has no
    interceptor, so without this a ``metric_compute`` result would hand its
    ``rows`` (the literal number) straight to the model — the exact
    anti-hallucination breach the catalog exists to prevent.

    This applies ONLY to payloads that opted in via ``suppress_llm_value`` (the
    metric data_table). Every other tool result passes through byte-identical, so
    normal SuiteQL/data tables keep their rows on this path exactly as before.
    Uses the SAME condenser the streaming interceptor uses, so the two paths
    cannot drift on what the LLM sees for a metric.
    """
    from app.services.metrics.metric_compute import (
        condense_metric_for_llm,
        is_suppressed_metric_payload,
    )

    try:
        parsed = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return result_str
    if is_suppressed_metric_payload(parsed):
        return condense_metric_for_llm(parsed)
    return result_str


def _truncate_tool_result(result_str: str) -> str:
    """Truncate tool results to prevent token bloat.

    Handles both error payloads (truncate message) and large success payloads
    (cap rows at _MAX_RESULT_ROWS). This prevents the LLM from choking on
    hundreds of raw data rows.
    """
    try:
        parsed = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        # Not JSON — truncate if very large
        if len(result_str) > _MAX_ERROR_CHARS * 3:
            return result_str[:_MAX_ERROR_CHARS] + "\n... (truncated)"
        return result_str

    if not isinstance(parsed, dict):
        return result_str

    # Truncate error responses
    is_error = parsed.get("error") is True or (isinstance(parsed.get("error"), str) and parsed["error"])
    if is_error:
        for key in ("message", "error_message", "detail"):
            if key in parsed and isinstance(parsed[key], str) and len(parsed[key]) > _MAX_ERROR_CHARS:
                parsed[key] = parsed[key][:_MAX_ERROR_CHARS] + "... (truncated)"
        return json.dumps(parsed, default=str)

    # Cap large row-based results (e.g., SuiteQL queries returning hundreds of rows)
    rows = parsed.get("rows")
    if isinstance(rows, list) and len(rows) > _MAX_RESULT_ROWS:
        original_count = len(rows)
        parsed["rows"] = rows[:_MAX_RESULT_ROWS]
        parsed["row_count"] = original_count
        parsed["rows_truncated"] = True
        parsed["rows_shown"] = _MAX_RESULT_ROWS
        parsed["_warning"] = (
            f"Only first {_MAX_RESULT_ROWS} of {original_count} rows shown. "
            f"Use GROUP BY with aggregate functions (COUNT, SUM) to get summaries "
            f"instead of fetching individual rows."
        )
        return json.dumps(parsed, default=str)

    # Also cap large "items" arrays (alternative result format)
    items = parsed.get("items")
    if isinstance(items, list) and len(items) > _MAX_RESULT_ROWS:
        original_count = len(items)
        parsed["items"] = items[:_MAX_RESULT_ROWS]
        parsed["items_truncated"] = True
        parsed["items_shown"] = _MAX_RESULT_ROWS
        parsed["total_items"] = original_count
        parsed["_warning"] = (
            f"Only first {_MAX_RESULT_ROWS} of {original_count} items shown. "
            f"Use GROUP BY with aggregate functions to get summaries."
        )
        return json.dumps(parsed, default=str)

    return result_str


# Backward-compatible alias
_truncate_error_payload = _truncate_tool_result


# Cache the decided arity per interceptor OBJECT (a WeakKeyDictionary, NOT id()-keyed:
# short-lived test closures can be GC'd and their id() reused, which would return a
# stale arity for a different function at the same address). Falls back to no caching
# for un-weakref-able callables.
_INTERCEPTOR_ARITY_CACHE: "weakref.WeakKeyDictionary[Any, int]" = weakref.WeakKeyDictionary()


def _interceptor_arity(interceptor) -> int:
    """Decide how many positional args an interceptor accepts: 4, 3, or 2.

    Computed ONCE per interceptor (cached by object) via ``inspect.signature`` — NOT
    by catching TypeErrors at the call boundary (re-gate r3, finding #3). The old
    try/except-TypeError ladder conflated an arity mismatch with a REAL TypeError
    raised INSIDE the interceptor body, silently re-running side-effecting,
    numbering-sensitive code (double-incrementing the result-id counter, re-writing
    the sidecar). Deciding arity by signature lets genuine TypeErrors propagate.
    """
    try:
        cached = _INTERCEPTOR_ARITY_CACHE.get(interceptor)
    except TypeError:
        cached = None  # un-hashable / un-weakref-able callable
    if cached is not None:
        return cached

    arity = 4  # production shape: (tool_name, result_str, params, full_result_str)
    try:
        sig = inspect.signature(interceptor)
        # If any parameter is VAR_POSITIONAL (*args), the callable accepts all 4.
        if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values()):
            arity = 4
        else:
            positional = [
                p
                for p in sig.parameters.values()
                if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            # Clamp into the supported {2, 3, 4} window: anything >=4 is the full shape;
            # 3 takes params; 2 (or fewer) is the minimal legacy shape.
            arity = min(max(len(positional), 2), 4)
    except (TypeError, ValueError):
        # Un-introspectable callable (builtin/C) — assume the full production shape.
        arity = 4

    try:
        _INTERCEPTOR_ARITY_CACHE[interceptor] = arity
    except TypeError:
        pass  # un-weakref-able callable — skip caching, recompute next call
    return arity


def _call_tool_result_interceptor(interceptor, tool_name, llm_result_str, params, full_result_str):
    """Invoke a tool-result interceptor, dispatching on its declared arity.

    The production interceptor (``orchestrator._make_tool_interceptor``) accepts
    ``(tool_name, result_str, params, full_result_str)`` — the LLM-facing string
    AND the ORIGINAL pre-truncation string (so the in-turn full-payload sidecar is
    uncapped, finding #10). Older/test interceptors may only take
    ``(tool_name, result_str)`` or ``(tool_name, result_str, params)``.

    Arity is decided ONCE via ``inspect.signature`` (cached) so a REAL TypeError
    raised INSIDE the interceptor body PROPAGATES instead of being swallowed and
    silently retried with fewer args (re-gate r3, finding #3).
    """
    arity = _interceptor_arity(interceptor)
    if arity >= 4:
        return interceptor(tool_name, llm_result_str, params, full_result_str)
    if arity == 3:
        return interceptor(tool_name, llm_result_str, params)
    return interceptor(tool_name, llm_result_str)


_CONFIDENCE_RE = re.compile(r"<confidence>(\d)</confidence>")
_LOW_CONFIDENCE_DISCLAIMER = (
    "\n\n*Note: I'm not fully confident in this result. Please verify the data before acting on it.*"
)


def parse_confidence(text: str) -> int | None:
    """Extract confidence score (1-5) from <confidence>N</confidence> tag."""
    match = _CONFIDENCE_RE.search(text)
    if match:
        return int(match.group(1))
    return None


_REASONING_RE = re.compile(r"<reasoning>.*?</reasoning>\s*", re.DOTALL)


def strip_confidence_tag(text: str) -> str:
    """Remove <confidence>N</confidence> and <reasoning>...</reasoning> from text."""
    text = _REASONING_RE.sub("", text)
    return _CONFIDENCE_RE.sub("", text).strip()


async def _maybe_store_query_pattern(
    db: "AsyncSession",
    tenant_id: "uuid.UUID",
    user_question: str,
    tool_calls_log: list[dict],
) -> None:
    """DEPRECATED — auto-pattern-learning disabled 2026-04-09.

    This was the source of the pattern-pollution feedback loop:
    live chat runs extracted any SuiteQL query that had GROUP BY and
    returned rows, with no verification of correctness. Combined with
    `query_pattern_similarity` in the confidence scorer, this created a
    self-reinforcing cycle where bad patterns boosted their own confidence
    on retrieval and spawned more bad patterns.

    Patterns now come exclusively from vetted sources:
      1. The nightly benchmark runner (`autonomous-improvement` skill)
         only promotes patterns that pass the golden eval suite.
      2. Manual admin seeds via `extract_and_store_pattern` with a
         known-good `tool_calls_log` (see `query_experiment_service`).
      3. Explicit user feedback → manual review → promotion.

    This function is kept as a no-op so existing call sites compile but
    do nothing. Do NOT re-enable auto-learning from live chat runs
    without eval-gated promotion in place. See
    docs/postmortem/2026-04-09-pattern-poisoning.md.
    """
    return


async def _resolve_default_workspace(
    db: "AsyncSession",
    tenant_id: "uuid.UUID",
) -> str | None:
    """Find the best active workspace for a tenant — prefers the one with most files."""
    from sqlalchemy import func, select

    from app.models.workspace import Workspace, WorkspaceFile

    result = await db.execute(
        select(Workspace.id, func.count(WorkspaceFile.id).label("file_count"))
        .outerjoin(WorkspaceFile, WorkspaceFile.workspace_id == Workspace.id)
        .where(Workspace.tenant_id == tenant_id, Workspace.status == "active")
        .group_by(Workspace.id)
        .order_by(func.count(WorkspaceFile.id).desc())
        .limit(1)
    )
    row = result.first()
    if row is None:
        return None
    print(f"[WORKSPACE] Resolved workspace {row[0]} ({row[1]} files)", flush=True)
    return str(row[0])


async def _ensure_valid_workspace_id(
    block_input: dict,
    db: "AsyncSession",
    tenant_id: "uuid.UUID",
) -> None:
    """Validate and resolve workspace_id on a tool call input dict.

    If the LLM-provided workspace_id is missing, invalid UUID, or doesn't
    belong to the tenant, resolves it to the best workspace (most files).
    """
    ws_id = block_input.get("workspace_id", "")
    needs_resolve = False
    if not ws_id or not _is_valid_uuid(ws_id):
        needs_resolve = True
    else:
        from sqlalchemy import select as _sel

        from app.models.workspace import Workspace as _Ws

        _ws_check = await db.execute(_sel(_Ws.id).where(_Ws.id == ws_id, _Ws.tenant_id == tenant_id))
        if _ws_check.scalar_one_or_none() is None:
            print(f"[WORKSPACE] LLM provided invalid workspace_id {ws_id}, resolving", flush=True)
            needs_resolve = True
    if needs_resolve:
        resolved = await _resolve_default_workspace(db, tenant_id)
        if resolved:
            block_input["workspace_id"] = resolved


@dataclass
class AgentResult:
    """Result from a specialist agent run."""

    success: bool
    data: Any = None  # Final text output or structured data
    error: str | None = None
    tool_calls_log: list[dict] = field(default_factory=list)
    tokens_used: TokenUsage = field(default_factory=TokenUsage)
    agent_name: str = ""
    confidence_score: float | None = None


def _compute_confidence(
    llm_confidence: int | None,
    context: dict[str, Any],
    tool_calls_log: list[dict],
) -> float:
    """Build a composite confidence score from all available signals."""
    llm_norm = (llm_confidence / 5.0) if llm_confidence else 0.0

    total_tools = len(tool_calls_log)
    successful_tools = sum(1 for t in tool_calls_log if not tool_call_had_error(t))
    tool_rate = (successful_tools / total_tools) if total_tools > 0 else 0.0

    # Any data tool call means the query required tools.
    # Data sources: data_table (SuiteQL/pivot), financial (reports),
    # bigquery (BQ SQL), rag (knowledge/web search).
    _DATA_CATEGORIES = {"data_table", "financial", "bigquery", "rag"}
    required = any(categorize(t.get("tool_name", "")) in _DATA_CATEGORIES for t in tool_calls_log)

    # Deterministic tools return factual data — success means high confidence by definition
    _DETERMINISTIC_TOOLS = {"netsuite_financial_report"}
    deterministic = any(
        t.get("tool_name") in _DETERMINISTIC_TOOLS and not tool_call_had_error(t) for t in tool_calls_log
    )

    return CompositeScorer(
        llm_score=llm_norm,
        query_pattern_similarity=context.get("matched_pattern_similarity", 0.0),
        query_pattern_success_count=context.get("matched_pattern_success_count", 0),
        domain_knowledge_similarity=context.get("domain_knowledge_similarity", 0.0),
        entity_resolution_confidence=context.get("entity_resolution_confidence", 0.0),
        tool_success_rate=tool_rate,
        num_tool_calls=total_tools,
        required_tool_calls=required,
        deterministic_success=deterministic,
    ).compute()


class BaseSpecialistAgent(abc.ABC):
    """Abstract base class for specialist agents.

    Subclasses must implement:
    - agent_name: identifier used in logs and coordinator dispatch
    - system_prompt: specialist-specific system prompt
    - tool_definitions: list of tools available to this agent (Anthropic format)
    - max_steps: maximum agentic loop iterations (default 3)
    """

    def __init__(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        correlation_id: str,
    ) -> None:
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.correlation_id = correlation_id
        # Set by run() / run_streaming() from the turn context. Consumed by
        # `build_current_date_block` in every agent's system_prompt so the LLM
        # always knows today's date regardless of which agent handles the turn.
        self._user_timezone: str | None = None

    @property
    @abc.abstractmethod
    def agent_name(self) -> str:
        """Identifier for this agent type (e.g. 'suiteql', 'rag', 'analysis')."""

    @property
    @abc.abstractmethod
    def system_prompt(self) -> str:
        """System prompt for this specialist."""

    @property
    @abc.abstractmethod
    def tool_definitions(self) -> list[dict]:
        """Tool definitions in Anthropic format available to this agent."""

    @property
    def max_steps(self) -> int:
        return 3

    async def run(
        self,
        task: str,
        context: dict[str, Any],
        db: AsyncSession,
        adapter: BaseLLMAdapter,
        model: str,
        tool_choice: dict | str | None = None,
        session_id: str | None = None,
        thinking_level: str | None = None,
    ) -> AgentResult:
        """Execute the specialist's mini agentic loop.

        Parameters
        ----------
        task : str
            The sub-task description from the coordinator.
        context : dict
            Additional context (e.g. prior agent results, conversation history).
        db : AsyncSession
            Database session for tool execution.
        adapter : BaseLLMAdapter
            LLM adapter to use (typically Haiku for specialists).
        model : str
            Model identifier to use.

        Returns
        -------
        AgentResult
            Contains the agent's output, tool call log, and token usage.
        """
        from app.services.chat.tools import execute_tool_call
        from app.services.policy_service import evaluate_tool_call as policy_evaluate
        from app.services.policy_service import get_active_policy, redact_output

        # Capture timezone from context so system_prompt can inject today's date
        self._user_timezone = context.get("user_timezone")

        # Carried thinking level: the loop reads this on every adapter call and
        # Task A5 bumps it when the model calls escalate_reasoning.
        # A forced tool_choice (only ever applied at step 0) suppresses thinking on
        # the first hop — so the turn MUST run thinking-off throughout, else a later
        # hop re-enabling thinking would 400 on the blockless step-0 history.
        current_thinking_level = "none" if thinking.is_forced_tool_choice(tool_choice) else thinking_level

        tool_calls_log: list[dict] = []
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_creation = 0
        total_cache_read = 0

        # Load policy for tool gating
        active_policy = await get_active_policy(db, self.tenant_id)

        # Build initial messages
        context_block = ""
        if context.get("prior_results"):
            prior = json.dumps(context["prior_results"], default=str)
            context_block = f"\n\n<prior_agent_results>\n{prior}\n</prior_agent_results>"

        messages: list[dict] = [
            {
                "role": "user",
                "content": f"Task: {task}{context_block}",
            }
        ]

        tools = self.tool_definitions if self.tool_definitions else None

        # Inject learned rules into system prompt for all agents
        _system_prompt = self.system_prompt
        _system_prompt += _build_learned_rules_block(context.get("learned_rules", []))

        prompt_parts = split_system_prompt(_system_prompt)

        try:
            for step in range(self.max_steps):
                step_tool_choice = tool_choice if step == 0 else None
                response: LLMResponse = await adapter.create_message(
                    model=model,
                    max_tokens=16384,
                    system=prompt_parts.static,
                    system_dynamic=prompt_parts.dynamic,
                    messages=messages,
                    tools=tools,
                    tool_choice=step_tool_choice,
                    thinking_level=current_thinking_level,
                )
                total_input_tokens += response.usage.input_tokens
                total_output_tokens += response.usage.output_tokens
                total_cache_creation += response.usage.cache_creation_input_tokens
                total_cache_read += response.usage.cache_read_input_tokens

                # Pure text response — agent is done
                if not response.tool_use_blocks:
                    # Guard: if step 0 and task contains a SELECT query, the model
                    # is hallucinating from conversation history instead of executing.
                    # Force it to actually call the tool.
                    if step == 0 and tool_calls_log == [] and _task_contains_query(task):
                        print(f"[AGENT] {self.agent_name} skipped tool on data query — forcing execution", flush=True)
                        messages.append(adapter.build_assistant_message(response))
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "You MUST execute the query using netsuite_suiteql — do NOT answer from memory "
                                    "or prior conversation. The user needs fresh, live data from NetSuite. "
                                    "Call the tool NOW."
                                ),
                            }
                        )
                        continue

                    final_text = "\n".join(response.text_blocks) if response.text_blocks else ""

                    # Extract confidence BEFORE stripping tag so agent self-score is used
                    # (Haiku fallback only fires when tag is missing)
                    tools_used = [c.get("tool", "") for c in tool_calls_log]
                    tool_ok = sum(1 for c in tool_calls_log if not tool_call_had_error(c))
                    tool_rate = tool_ok / len(tool_calls_log) if tool_calls_log else 0.0

                    assessment = await extract_structured_confidence(
                        user_question=task,
                        assistant_response=final_text[:500],
                        tools_used=tools_used,
                        tool_success_rate=tool_rate,
                    )
                    confidence = assessment.score
                    final_text = strip_confidence_tag(final_text)
                    if confidence <= 2:
                        final_text += _LOW_CONFIDENCE_DISCLAIMER
                    logger.info(
                        "agent.confidence agent=%s score=%d source=%s", self.agent_name, confidence, assessment.source
                    )

                    composite = _compute_confidence(confidence, context, tool_calls_log)

                    # Auto-extract query patterns (fire-and-forget)
                    await _maybe_store_query_pattern(db, self.tenant_id, task, tool_calls_log)

                    return AgentResult(
                        success=True,
                        data=final_text,
                        tool_calls_log=tool_calls_log,
                        tokens_used=TokenUsage(
                            total_input_tokens, total_output_tokens, total_cache_creation, total_cache_read
                        ),
                        agent_name=self.agent_name,
                        confidence_score=composite,
                    )

                # Process tool calls
                messages.append(adapter.build_assistant_message(response))

                tool_results_content = []
                for block in response.tool_use_blocks:
                    if block.name.startswith("workspace_"):
                        await _ensure_valid_workspace_id(block.input, db, self.tenant_id)

                    # Layer-2 escalation: the model asked for deeper reasoning.
                    # Only RAISE depth on a turn that ALREADY has thinking on. Never
                    # flip none->on mid-turn: prior assistant turns lack thinking
                    # blocks + the temperature flip would 400, and a none level means
                    # thinking is globally off (kill-switch) or this is a simple
                    # lookup that shouldn't think.
                    if block.name == "escalate_reasoning" and thinking.budget_for(current_thinking_level) > 0:
                        # Key on the ACTUAL budget, not the level string: a level whose
                        # budget is 0 (none, or a misconfigured/unknown level) means
                        # thinking is off this turn, so bumping would flip none->on
                        # mid-turn against a blockless history → 400. Only raise when
                        # thinking is genuinely active.
                        current_thinking_level = thinking.next_level(current_thinking_level)

                    t0 = time.monotonic()

                    # Mutation intercept: block writes in non-streaming path too
                    from app.services.chat.mutation_guard import classify_mutation as _classify_mut

                    _mut_type = _classify_mut(block.name)
                    if _mut_type is not None:
                        result_str = json.dumps(
                            {
                                "error": "Write operations require the streaming chat path for HITL confirmation. "
                                "This tool cannot be executed in the non-streaming path.",
                                "blocked": True,
                            }
                        )
                        elapsed_ms = int((time.monotonic() - t0) * 1000)
                        tool_calls_log.append(
                            build_tool_call_log_entry(
                                step=step,
                                agent_name=self.agent_name,
                                tool_name=block.name,
                                params=block.input,
                                result_str=result_str,
                                duration_ms=elapsed_ms,
                            )
                        )
                        tool_results_content.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_str,
                            }
                        )
                        continue

                    # Policy check
                    policy_result = policy_evaluate(active_policy, block.name, block.input)
                    if not policy_result["allowed"]:
                        result_str = json.dumps(
                            {"error": f"Policy blocked: {policy_result.get('reason', 'Not allowed')}"}
                        )
                    else:
                        result_str = await execute_tool_call(
                            tool_name=block.name,
                            tool_input=block.input,
                            tenant_id=self.tenant_id,
                            actor_id=self.user_id,
                            correlation_id=self.correlation_id,
                            db=db,
                            context_need=getattr(self, "_context_need", None),
                            session_id=session_id,
                        )

                        # Output redaction
                        if active_policy and active_policy.blocked_fields:
                            try:
                                parsed = json.loads(result_str)
                                parsed = redact_output(active_policy, parsed)
                                result_str = json.dumps(parsed, default=str)
                            except (json.JSONDecodeError, TypeError):
                                pass

                    # Truncate error payloads to prevent token bloat on retries
                    result_str = _truncate_error_payload(result_str)

                    # Metric trust boundary on the non-streaming path: the streaming
                    # interceptor is absent here, so suppress a metric's computed number
                    # from the LLM-facing content directly (anti-hallucination invariant).
                    # The full result_str is still recorded in the audit log below.
                    llm_result_str = _suppress_metric_value_for_llm(result_str)

                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    tool_calls_log.append(
                        build_tool_call_log_entry(
                            step=step,
                            agent_name=self.agent_name,
                            tool_name=block.name,
                            params=block.input,
                            result_str=result_str,
                            duration_ms=elapsed_ms,
                        )
                    )

                    tool_results_content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": llm_result_str,
                        }
                    )

                messages.append(adapter.build_tool_result_message(tool_results_content))

            # Loop exhausted — make one final call without tools (must answer)
            print(
                f"[AGENT] {self.agent_name} loop exhausted {self.max_steps} steps, forcing final response",
                flush=True,
            )
            logger.warning(
                "Agent %s loop exhausted %d steps, forcing final response",
                self.agent_name,
                self.max_steps,
            )
            messages.append(
                {
                    "role": "user",
                    "content": "You have used all available tool steps. You MUST now provide your final answer to the user based on everything you have gathered so far. Do NOT output only reasoning — give the user a clear, helpful response.",
                }
            )
            response = await adapter.create_message(
                model=model,
                max_tokens=16384,
                system=prompt_parts.static,
                system_dynamic=prompt_parts.dynamic,
                messages=messages,
                thinking_level=current_thinking_level,
            )
            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens
            total_cache_creation += response.usage.cache_creation_input_tokens
            total_cache_read += response.usage.cache_read_input_tokens
            final_text = "\n".join(response.text_blocks) if response.text_blocks else ""

            # Extract confidence BEFORE stripping tag so agent self-score is used
            # (Haiku fallback only fires when tag is missing)
            tools_used = [c.get("tool", "") for c in tool_calls_log]
            tool_ok = sum(1 for c in tool_calls_log if not tool_call_had_error(c))
            tool_rate = tool_ok / len(tool_calls_log) if tool_calls_log else 0.0

            assessment = await extract_structured_confidence(
                user_question=task,
                assistant_response=final_text[:500],
                tools_used=tools_used,
                tool_success_rate=tool_rate,
            )
            confidence = assessment.score
            final_text = strip_confidence_tag(final_text)
            if confidence <= 2:
                final_text += _LOW_CONFIDENCE_DISCLAIMER
            logger.info("agent.confidence agent=%s score=%d source=%s", self.agent_name, confidence, assessment.source)

            composite = _compute_confidence(confidence, context, tool_calls_log)

            await _maybe_store_query_pattern(db, self.tenant_id, task, tool_calls_log)

            return AgentResult(
                success=True,
                data=final_text,
                tool_calls_log=tool_calls_log,
                tokens_used=TokenUsage(total_input_tokens, total_output_tokens, total_cache_creation, total_cache_read),
                agent_name=self.agent_name,
                confidence_score=composite,
            )

        except Exception as exc:
            logger.error("Agent %s failed: %s", self.agent_name, exc, exc_info=True)
            return AgentResult(
                success=False,
                error=str(exc),
                tool_calls_log=tool_calls_log,
                tokens_used=TokenUsage(total_input_tokens, total_output_tokens, total_cache_creation, total_cache_read),
                agent_name=self.agent_name,
            )

    async def run_streaming(
        self,
        task: str,
        context: dict[str, Any],
        db: "AsyncSession",
        adapter: "BaseLLMAdapter",
        model: str,
        conversation_history: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        tool_result_interceptor: Callable[..., tuple[tuple[str, dict] | None, str]] | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        thinking_level: str | None = None,
    ):
        """Execute the agentic loop with streaming text output.

        Yields events:
        - ("text", chunk) — text token from the LLM stream
        - ("tool_status", message) — tool execution status
        - ("tool_intercept", data) — intercepted tool result (event_type, event_data) tuple
        - ("response", AgentResult) — final result when done

        ``tool_result_interceptor`` is an optional callback
        ``(tool_name, result_str) -> ((event_type, event_data) | None, result_str)``.
        When it returns non-None, a ``("tool_intercept", (event_type, event_data))`` event
        is yielded and the (possibly modified) result_str is used for subsequent LLM context.
        """
        from app.services.chat.tools import execute_tool_call
        from app.services.policy_service import evaluate_tool_call as policy_evaluate
        from app.services.policy_service import get_active_policy, redact_output

        # Capture timezone from context so system_prompt can inject today's date
        self._user_timezone = context.get("user_timezone")

        # Carried thinking level: the loop reads this on every adapter call and
        # Task A5 bumps it when the model calls escalate_reasoning.
        # A forced tool_choice (only ever applied at step 0) suppresses thinking on
        # the first hop — so the turn MUST run thinking-off throughout, else a later
        # hop re-enabling thinking would 400 on the blockless step-0 history.
        current_thinking_level = "none" if thinking.is_forced_tool_choice(tool_choice) else thinking_level

        tool_calls_log: list[dict] = []
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_creation = 0
        total_cache_read = 0

        active_policy = await get_active_policy(db, self.tenant_id)

        context_block = ""
        if context.get("prior_results"):
            prior = json.dumps(context["prior_results"], default=str)
            context_block = f"\n\n<prior_agent_results>\n{prior}\n</prior_agent_results>"

        # Build messages: include conversation history for multi-turn context
        messages: list[dict] = []
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": f"Task: {task}{context_block}"})

        tools = self.tool_definitions if self.tool_definitions else None

        # Inject learned rules into system prompt for all agents
        _system_prompt = self.system_prompt
        _system_prompt += _build_learned_rules_block(context.get("learned_rules", []))

        prompt_parts = split_system_prompt(_system_prompt)

        try:
            patched_files: set[str] = set()  # Dedup workspace_propose_patch per file
            for step in range(self.max_steps):
                # Check cancel flag between steps (background run graceful stop)
                if run_id and step > 0:
                    from app.services.chat.run_manager import get_run_manager

                    rm = get_run_manager()
                    if rm.is_cancelled(run_id):
                        logger.info("Agent cancelled at step %d for run %s", step, run_id)
                        yield "text", "\n\n*(Response cancelled)*"
                        return

                # Stream the LLM response
                step_tool_choice = tool_choice if step == 0 else None
                response = None
                async for event_type, payload in adapter.stream_message(
                    model=model,
                    max_tokens=16384,
                    system=prompt_parts.static,
                    system_dynamic=prompt_parts.dynamic,
                    messages=messages,
                    tools=tools,
                    tool_choice=step_tool_choice,
                    thinking_level=current_thinking_level,
                ):
                    if event_type == "text":
                        yield "text", payload
                    elif event_type == "response":
                        response = payload

                if not response:
                    yield "text", "\n\nI'm sorry, the response timed out. Please try again with a simpler question."
                    print(f"[AGENT] {self.agent_name} stream returned no response (possible timeout)", flush=True)
                    break

                total_input_tokens += response.usage.input_tokens
                total_output_tokens += response.usage.output_tokens
                total_cache_creation += response.usage.cache_creation_input_tokens
                total_cache_read += response.usage.cache_read_input_tokens

                # Pure text response — done
                if not response.tool_use_blocks:
                    # Guard: if step 0 and task contains a SELECT query, the model
                    # is hallucinating from conversation history instead of executing.
                    # Force it to actually call the tool.
                    if step == 0 and tool_calls_log == [] and _task_contains_query(task):
                        print(f"[AGENT] {self.agent_name} skipped tool on data query — forcing execution", flush=True)
                        messages.append(adapter.build_assistant_message(response))
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "You MUST execute the query using netsuite_suiteql — do NOT answer from memory "
                                    "or prior conversation. The user needs fresh, live data from NetSuite. "
                                    "Call the tool NOW."
                                ),
                            }
                        )
                        continue

                    final_text = "\n".join(response.text_blocks) if response.text_blocks else ""

                    # Extract confidence BEFORE stripping tag so agent self-score is used
                    # (Haiku fallback only fires when tag is missing)
                    tools_used = [c.get("tool", "") for c in tool_calls_log]
                    tool_ok = sum(1 for c in tool_calls_log if not tool_call_had_error(c))
                    tool_rate = tool_ok / len(tool_calls_log) if tool_calls_log else 0.0

                    assessment = await extract_structured_confidence(
                        user_question=task,
                        assistant_response=final_text[:500],
                        tools_used=tools_used,
                        tool_success_rate=tool_rate,
                    )
                    confidence = assessment.score
                    final_text = strip_confidence_tag(final_text)
                    if confidence <= 2:
                        final_text += _LOW_CONFIDENCE_DISCLAIMER
                    logger.info(
                        "agent.confidence agent=%s score=%d source=%s", self.agent_name, confidence, assessment.source
                    )

                    composite = _compute_confidence(confidence, context, tool_calls_log)

                    await _maybe_store_query_pattern(db, self.tenant_id, task, tool_calls_log)

                    yield (
                        "response",
                        AgentResult(
                            success=True,
                            data=final_text,
                            tool_calls_log=tool_calls_log,
                            tokens_used=TokenUsage(
                                total_input_tokens, total_output_tokens, total_cache_creation, total_cache_read
                            ),
                            agent_name=self.agent_name,
                            confidence_score=composite,
                        ),
                    )
                    return

                # Process tool calls
                messages.append(adapter.build_assistant_message(response))
                tool_results_content = []
                raw_result_strings: list[str] = []  # Track originals for stop-when-done check

                for i, block in enumerate(response.tool_use_blocks):
                    if block.name.startswith("workspace_"):
                        await _ensure_valid_workspace_id(block.input, db, self.tenant_id)

                    # Layer-2 escalation: the model asked for deeper reasoning.
                    # Only RAISE depth on a turn that ALREADY has thinking on. Never
                    # flip none->on mid-turn: prior assistant turns lack thinking
                    # blocks + the temperature flip would 400, and a none level means
                    # thinking is globally off (kill-switch) or this is a simple
                    # lookup that shouldn't think.
                    if block.name == "escalate_reasoning" and thinking.budget_for(current_thinking_level) > 0:
                        # Key on the ACTUAL budget, not the level string: a level whose
                        # budget is 0 (none, or a misconfigured/unknown level) means
                        # thinking is off this turn, so bumping would flip none->on
                        # mid-turn against a blockless history → 400. Only raise when
                        # thinking is genuinely active.
                        current_thinking_level = thinking.next_level(current_thinking_level)

                    # Dedup: skip duplicate workspace_propose_patch for same file
                    if block.name == "workspace_propose_patch":
                        file_path = block.input.get("file_path", "")
                        if file_path in patched_files:
                            print(f"[WORKSPACE] Skipping duplicate patch for {file_path}", flush=True)
                            tool_results_content.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": json.dumps(
                                        {
                                            "skipped": "Already proposed a patch for this file. "
                                            "Show the diff and present results."
                                        }
                                    ),
                                }
                            )
                            continue
                        patched_files.add(file_path)

                    yield "tool_status", f"Executing {block.name}..."
                    yield (
                        "tool_start",
                        {
                            "tool_name": block.name,
                            "tool_input": block.input,
                            "step": step,
                        },
                    )

                    t0 = time.monotonic()

                    # ── Plan Mode clarify intercept (TERMINAL) ──
                    # When the agent calls the `clarify` tool we short-circuit
                    # the turn: validate the schema, emit a clarification_required
                    # SSE event, and return a synthetic empty response. On
                    # validation failure, feed the error back to the agent as a
                    # tool_result(is_error=True) so it can retry within the turn.
                    if block.name == "clarify":
                        from app.services.chat.plan_mode.clarify_intercept import (
                            InterceptError,
                            InterceptResult,
                            intercept_clarify_call,
                        )
                        from app.services.connection_service import list_connections

                        # Active connectors = MCP connectors + REST connections.
                        # REST-only tenants (e.g., NetSuite via REST API without an
                        # MCP connector) would otherwise be excluded from the
                        # canonical-source set and every clarify option would drop.
                        _mcp_providers = [getattr(c, "provider", "") for c in getattr(self, "_connectors", [])]
                        _rest_connections: list = []
                        try:
                            _rest_connections = await list_connections(db, self.tenant_id)
                        except Exception:
                            logger.warning("clarify_intercept.rest_connections_failed", exc_info=True)
                        _rest_providers = [
                            getattr(c, "provider", "")
                            for c in _rest_connections
                            if getattr(c, "status", "active") == "active"
                        ]
                        _connector_providers = [*_mcp_providers, *_rest_providers]
                        clar_result = await intercept_clarify_call(
                            tool_input=block.input,
                            session_id=session_id or str(self.tenant_id),
                            active_connectors=_connector_providers,
                            db=db,
                        )

                        if isinstance(clar_result, InterceptError):
                            # Feed error back to agent — let it retry within the turn
                            result_str = json.dumps({"error": clar_result.error_message, "retry": True})
                            elapsed_ms = int((time.monotonic() - t0) * 1000)
                            yield (
                                "tool_end",
                                {
                                    "tool_name": block.name,
                                    "step": step,
                                    "duration_ms": elapsed_ms,
                                    "success": False,
                                    "result_summary": "Clarify schema invalid",
                                },
                            )
                            tool_calls_log.append(
                                build_tool_call_log_entry(
                                    step=step,
                                    agent_name=self.agent_name,
                                    tool_name=block.name,
                                    params=block.input,
                                    result_str=result_str,
                                    duration_ms=elapsed_ms,
                                )
                            )
                            tool_results_content.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": result_str,
                                    "is_error": True,
                                }
                            )
                            continue

                        # InterceptResult — emit clarification_required + terminal response
                        assert isinstance(clar_result, InterceptResult)
                        yield ("clarification_required", clar_result.sse_payload)
                        elapsed_ms = int((time.monotonic() - t0) * 1000)
                        yield (
                            "tool_end",
                            {
                                "tool_name": block.name,
                                "step": step,
                                "duration_ms": elapsed_ms,
                                "success": True,
                                "result_summary": "Clarification card shown",
                            },
                        )
                        tool_calls_log.append(
                            build_tool_call_log_entry(
                                step=step,
                                agent_name=self.agent_name,
                                tool_name=block.name,
                                params=block.input,
                                result_str=json.dumps({"sent_to_user": True}),
                                duration_ms=elapsed_ms,
                            )
                        )
                        # Terminal — emit final response with empty data and return
                        yield (
                            "response",
                            AgentResult(
                                success=True,
                                data="",
                                tool_calls_log=tool_calls_log,
                                tokens_used=TokenUsage(
                                    total_input_tokens,
                                    total_output_tokens,
                                    total_cache_creation,
                                    total_cache_read,
                                ),
                                agent_name=self.agent_name,
                                confidence_score=None,
                            ),
                        )
                        return
                    # ── End Plan Mode clarify intercept ──

                    # ── Mutation intercept: HITL write confirmation ──
                    from app.services.chat.mutation_guard import classify_mutation

                    mutation_type = classify_mutation(block.name)
                    if mutation_type is not None:
                        record_type = block.input.get("recordType", "unknown")

                        # ── ask_user hint pop (requirement C) — MUST happen
                        # before anything else touches tool_input: this key
                        # must never reach NetSuite, the signed HMAC
                        # envelope, or execute_tool_call. The model may name
                        # field NAMES ONLY it wants a human to choose; every
                        # VALUE offered to the human comes from a
                        # server-executed fetch (slot_option_sources.py),
                        # resolved below once validation/the repair loop have
                        # decided this attempt is the one shown as a card.
                        _ask_user_hint = block.input.pop("ask_user", None)

                        # Is this write headed for NetSuite at all? Everything
                        # the agentic write loop adds below — the investigation
                        # gate, validate_mutation, the ask_user slot fetch —
                        # is built entirely out of NetSuite sibling tools
                        # (ns_getRecordTypeMetadata, ns_getSubsidiaries). A
                        # Celigo connector has none of them, so running any of
                        # it there bounces the write with an instruction to
                        # call a tool that does not exist in its toolset, and
                        # the write can never proceed. Same `ns_` test the
                        # ns_getRecord pre-fetch below already uses; a
                        # non-NetSuite write keeps `validation = None` and
                        # reaches the confirmation card exactly as it did
                        # before this loop existed.
                        from app.services.chat.tools import (
                            parse_external_tool_name as _parse_write_tool_name,
                        )

                        _parsed_write = _parse_write_tool_name(block.name)
                        _is_netsuite_write = bool(_parsed_write and _parsed_write[1].startswith("ns_"))

                        # ── Investigation gate (requirement A) — mechanism,
                        # not prompt (the write profile's metadata-first
                        # prose has been ignored live on this branch before).
                        # Only create/upsert are gated: partial payloads are
                        # legitimate on update, and metadata cannot yield
                        # required fields anyway (see write_validator.py's
                        # honesty rule), so no pre-flight check could ever
                        # prove a create complete — the gate enforces
                        # BEHAVIOR (look before composing), the human
                        # enforces correctness. Bounded by construction: at
                        # most ONE bounce per (turn, record_type), tracked in
                        # a per-instance set — a stubborn model's SECOND
                        # proposal always reaches validation/the card.
                        if _is_netsuite_write and mutation_type in ("create", "upsert"):
                            if not hasattr(self, "_investigation_gate_bounced"):
                                self._investigation_gate_bounced: set[str] = set()
                            if record_type not in self._investigation_gate_bounced and not _metadata_fetched_this_turn(
                                tool_calls_log, record_type
                            ):
                                self._investigation_gate_bounced.add(record_type)
                                result_str = json.dumps(
                                    {
                                        "unexamined_write": True,
                                        "instruction": (
                                            f"Call ns_getRecordTypeMetadata for '{record_type}' first, "
                                            "resolve any values you need (e.g. ns_getSubsidiaries, or a "
                                            "SuiteQL lookup), then re-propose this write. If a value the "
                                            "record needs has several valid options and the user's request "
                                            "does not say which, do NOT pick one — add "
                                            "'ask_user': ['<field name>'] to the write call so the user "
                                            "is shown the real options."
                                        ),
                                    }
                                )
                                elapsed_ms = int((time.monotonic() - t0) * 1000)
                                yield (
                                    "tool_end",
                                    {
                                        "tool_name": block.name,
                                        "step": step,
                                        "duration_ms": elapsed_ms,
                                        "success": False,
                                        "result_summary": (
                                            f"Investigation required — call ns_getRecordTypeMetadata for "
                                            f"'{record_type}' before composing this {mutation_type}."
                                        ),
                                    },
                                )
                                tool_calls_log.append(
                                    build_tool_call_log_entry(
                                        step=step,
                                        agent_name=self.agent_name,
                                        tool_name=block.name,
                                        params=block.input,
                                        result_str=result_str,
                                        duration_ms=elapsed_ms,
                                    )
                                )
                                tool_results_content.append(
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": result_str,
                                        "is_error": True,
                                    }
                                )
                                continue
                        # ── End investigation gate ──

                        # ── Write validation + bounded repair ──
                        if not hasattr(self, "_write_repair"):
                            self._write_repair = WriteRepairState(max_attempts=2)

                        # validate_mutation() is the single entry point that
                        # owns normalize -> get_record_metadata ->
                        # check_posting_invariants -> validate_write,
                        # INCLUDING the delete shape (`{recordType, id}`, no
                        # `data`/`body` key) — a delete used to raise
                        # PayloadParseError out of normalize_write_payload, a
                        # function whose contract it was never meant to
                        # meet, which silently skipped this entire block for
                        # every delete. See write_validation.py.
                        validation = None
                        if _is_netsuite_write:
                            try:
                                validation = await validate_mutation(
                                    tool_name=block.name,
                                    tool_input=block.input,
                                    mutation_type=mutation_type,
                                    record_type=record_type,
                                    tenant_id=self.tenant_id,
                                    actor_id=self.user_id,
                                    correlation_id=self.correlation_id,
                                    db=db,
                                    session_id=session_id or str(self.tenant_id),
                                )
                            except PayloadParseError as exc:
                                result_str = json.dumps({"error": f"Write payload could not be parsed: {exc}"})
                                validation = None

                        # ── ask_user resolution (requirement C) — MUST run
                        # BEFORE the repair decision below, not after it.
                        # `ask_user` is the model stating it CANNOT determine
                        # a value and needs a human to choose. Deciding to
                        # repair first would send exactly that field back to
                        # the model to resolve on its own — the one action it
                        # has just told us cannot work — so the proposal would
                        # bounce, re-propose, and burn its repair budget to a
                        # stall while the human who could answer in one click
                        # never sees a card. Resolving first lets
                        # `with_delegated_slots` reclassify a resolved field
                        # from "missing" to "asked", which is what keeps the
                        # question in front of the person who can answer it.
                        #
                        # Cost: a hint on a proposal that ends up bounced
                        # anyway spends its option fetch for nothing. That is
                        # accepted — the fetch is one cached MCP call, and it
                        # is the only way to know whether the hint actually
                        # covers the gap before ruling on it.
                        #
                        # The model contributes a NAME ONLY; every VALUE comes
                        # from a server-executed fetch (slot_option_sources.py)
                        # — see that module's docstring for the full boundary.
                        _ask_user_rejected: list[dict[str, str]] = []

                        # Which field names might become a slot on this card.
                        #
                        # Two sources, and the second is what makes the form
                        # reachable at all. (1) names the MODEL asked about via
                        # its ask_user hint. (2) names the SERVER already knows
                        # are required, are missing from this payload, and have
                        # a server-side option source — i.e. there is a real
                        # list of valid values a human can just pick from.
                        #
                        # For (2) there is nothing left for the model to
                        # discover: it can only guess, and guessing is exactly
                        # what the confirmation card exists to prevent on a
                        # financial write. Relying on the model to volunteer
                        # ask_user did not work in practice either — live on
                        # staging it narrated the options in prose, or called
                        # ns_selector_app and announced a picker the UI cannot
                        # render, so the form was effectively unreachable.
                        #
                        # The repair loop keeps every gap a human CANNOT pick
                        # from a list (a company name, a line-level field):
                        # those still bounce to the model, which is the only
                        # party that can resolve them.
                        _hint_names = (
                            [n for n in _ask_user_hint if isinstance(n, str) and n]
                            if isinstance(_ask_user_hint, list)
                            else []
                        )
                        _slot_names = list(_hint_names)
                        if validation is not None:
                            from app.services.chat.slot_option_sources import (
                                is_option_sourced as _is_option_sourced,
                            )

                            for _missing in validation.missing_required:
                                if _missing not in _slot_names and _is_option_sourced(_missing):
                                    _slot_names.append(_missing)

                        if _slot_names and validation is not None:
                            from app.services.chat.slot_option_sources import resolve_ask_user_slots
                            from app.services.chat.write_validation import resolve_curated_metadata

                            # MUST be the curated metadata, not raw
                            # get_record_metadata — a T2 gate round found
                            # these two paths split, so a field could be
                            # reported `missing_required` by validation and
                            # simultaneously rejected here as "not a
                            # recognized field", bouncing to the repair loop
                            # the exact question meant to reach a human.
                            # Served from the same (connector_id,
                            # record_type) 1h cache validate_mutation just
                            # populated, so this is a cache hit, not a second
                            # live MCP round trip.
                            _ask_user_metadata = await resolve_curated_metadata(
                                tool_name=block.name,
                                tool_input=block.input,
                                mutation_type=mutation_type,
                                record_type=record_type,
                                tenant_id=self.tenant_id,
                                actor_id=self.user_id,
                                correlation_id=self.correlation_id,
                                db=db,
                                session_id=session_id or str(self.tenant_id),
                            )
                            _ask_user_slots, _ask_user_rejected = await resolve_ask_user_slots(
                                _slot_names,
                                metadata=_ask_user_metadata,
                                mutation_tool_name=block.name,
                                tenant_id=self.tenant_id,
                                actor_id=self.user_id,
                                correlation_id=self.correlation_id,
                                db=db,
                                session_id=session_id or str(self.tenant_id),
                                # Only slots that ALREADY carry a real
                                # allow-set count as declared. A slot
                                # `validate_write` derived for a missing
                                # required field has `allowed=None` (the live
                                # metadata shape has no options), which is
                                # unusable to a human — they would have to
                                # type a NetSuite internal id from memory.
                                # Treating those as declared would skip the
                                # fetch that is the entire point of the hint;
                                # `with_delegated_slots` replaces the bare
                                # slot with the resolved one by name.
                                already_declared=[s.name for s in validation.editable_slots if s.allowed],
                            )
                            if _ask_user_slots:
                                validation = validation.with_delegated_slots(_ask_user_slots)
                        # ── End ask_user resolution ──

                        if validation is not None:
                            if self._write_repair.should_repair(record_type, validation):
                                # Hand the model a structured error INSTEAD of a
                                # card. The human never sees an invalid payload.
                                # Same "feed error back to agent, retry within
                                # the turn" shape as the Plan Mode InterceptError
                                # branch above — result_str is what recomposes
                                # the model's next attempt, not an SSE event.
                                _model_error = validation.as_model_error()
                                if _ask_user_rejected:
                                    # The card path reports rejected hints to
                                    # the model; this path must too. Without
                                    # it a model whose hint named a bad field
                                    # gets bounced with no clue WHY the
                                    # delegation did not happen, so its next
                                    # attempt repeats the same bad name and
                                    # the repair budget drains on a mistake
                                    # we already diagnosed.
                                    # Front of the dict on purpose: the
                                    # persisted `result_summary` is truncated,
                                    # and a diagnostic that only exists past
                                    # the cut is invisible to anyone reading
                                    # the tool-call log afterwards.
                                    _model_error = {
                                        "unresolved_ask_user_fields": _ask_user_rejected,
                                        **_model_error,
                                    }
                                result_str = json.dumps(_model_error)
                                elapsed_ms = int((time.monotonic() - t0) * 1000)
                                # success MUST be False here — a repair round that
                                # logs as a success would make the whole loop
                                # invisible in the tool-call log. The summary
                                # names what actually failed (not exit_reason:
                                # that field stays None on this branch by design
                                # — the loop hasn't exited, it's mid-repair —
                                # so it would misreport rather than diagnose).
                                _failure_detail = _validation_failure_detail(validation)
                                yield (
                                    "tool_end",
                                    {
                                        "tool_name": block.name,
                                        "step": step,
                                        "duration_ms": elapsed_ms,
                                        "success": False,
                                        "result_summary": f"Write repair requested ({_failure_detail})",
                                    },
                                )
                                tool_calls_log.append(
                                    build_tool_call_log_entry(
                                        step=step,
                                        agent_name=self.agent_name,
                                        tool_name=block.name,
                                        params=block.input,
                                        result_str=result_str,
                                        duration_ms=elapsed_ms,
                                    )
                                )
                                tool_results_content.append(
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": result_str,
                                        "is_error": True,
                                    }
                                )
                                continue

                        # Consumed by Task 7 when the card learns about slots.
                        self._last_validation = validation
                        # ── End write validation + bounded repair ──

                        # ── Repair-chain stamping + intent guard (D/E) —
                        # server-stamped from orchestrator-held repair
                        # context ONLY, never from block.input. A repair-turn
                        # proposal whose (mutation_type, record_type) differs
                        # from the root is NOT linked into the chain — it
                        # becomes a plainly fresh proposal with a fresh
                        # budget (the intent guard).
                        _repair_context = getattr(self, "_write_repair_context", None)
                        _card_repair_of: str | None = None
                        _card_repair_attempt = 0
                        if (
                            _repair_context is not None
                            and _repair_context.get("mutation_type") == mutation_type
                            and _repair_context.get("record_type") == record_type
                        ):
                            _card_repair_of = _repair_context.get("root_id")
                            _card_repair_attempt = _repair_context.get("attempt", 0)
                        # ── End repair-chain stamping ──

                        # For updates/upserts: pre-fetch current record for
                        # before/after diff display (capped at 5s to avoid
                        # blocking the SSE stream on slow MCP calls)
                        current_record: dict[str, Any] | None = None
                        if mutation_type in ("update", "upsert"):
                            record_id = block.input.get("id") or (block.input.get("body") or {}).get("id")
                            if record_id:
                                from app.services.chat.tools import _make_ext_tool_name, parse_external_tool_name

                                _parsed = parse_external_tool_name(block.name)
                                # The pre-fetch only exists for NetSuite's ns_getRecord
                                # sibling tool. A non-NetSuite connector (e.g. Celigo) has
                                # no such tool, so skip the call instead of wasting up to
                                # 5s against the live MCP server before the except below
                                # would silently swallow the failure anyway.
                                if _parsed and _parsed[1].startswith("ns_"):
                                    get_tool_name = _make_ext_tool_name(_parsed[0], "ns_getRecord")
                                    try:
                                        import asyncio as _aio

                                        get_result_str = await _aio.wait_for(
                                            execute_tool_call(
                                                tool_name=get_tool_name,
                                                tool_input={"recordType": record_type, "id": str(record_id)},
                                                tenant_id=self.tenant_id,
                                                actor_id=self.user_id,
                                                correlation_id=self.correlation_id,
                                                db=db,
                                                session_id=session_id,
                                            ),
                                            timeout=5.0,
                                        )
                                        current_record = json.loads(get_result_str)
                                    except Exception:
                                        logger.warning(
                                            "mutation_intercept: failed to pre-fetch %s/%s",
                                            record_type,
                                            record_id,
                                        )

                        # Human labels for reference fields, so the card shows
                        # "Framework Computer UK Ltd (ID 5)" rather than
                        # {"id": "5"} on the one screen whose job is informed
                        # consent. Display-only and server-sourced; see
                        # reference_field_labels. NetSuite-only: the option
                        # sources it reads are ns_* tools.
                        _field_labels: dict[str, str] = {}
                        if _is_netsuite_write:
                            from app.services.chat.reference_field_labels import resolve_reference_labels
                            from app.services.chat.write_payload import (
                                PayloadParseError as _PPE,
                            )
                            from app.services.chat.write_validation import (
                                normalize_for_validation as _normalize_for_labels,
                            )

                            try:
                                _lbl_payload = _normalize_for_labels(mutation_type, block.input)
                                _field_labels = await resolve_reference_labels(
                                    _lbl_payload.fields,
                                    mutation_tool_name=block.name,
                                    tenant_id=self.tenant_id,
                                    actor_id=self.user_id,
                                    correlation_id=self.correlation_id,
                                    db=db,
                                    session_id=session_id or str(self.tenant_id),
                                )
                            except _PPE:
                                # An unparseable payload is handled below by
                                # build_confirmation_payload returning None;
                                # labels are cosmetic, so say nothing here.
                                _field_labels = {}

                        payload = build_confirmation_payload(
                            mutation_type=mutation_type,
                            record_type=record_type,
                            tool_name=block.name,
                            tool_input=block.input,
                            session_id=session_id if session_id else str(self.tenant_id),
                            current_record=current_record,
                            validation=getattr(self, "_last_validation", None),
                            repair_of=_card_repair_of,
                            repair_attempt=_card_repair_attempt,
                            field_labels=_field_labels,
                        )

                        payload_unparseable = False
                        if payload is None:
                            # `build_confirmation_payload` returns None for two distinct
                            # reasons: the record type is blocked/unknown, or the record
                            # type is allowed but the write payload could not be parsed.
                            # Re-check the allowlist to report the right one to the model.
                            from app.services.chat.mutation_guard import is_record_type_allowed

                            if is_record_type_allowed(record_type):
                                payload_unparseable = True
                                result_str = json.dumps(
                                    {
                                        "error": f"The write payload for this {mutation_type} operation on "
                                        f"{record_type} could not be read (missing or malformed data). "
                                        f"The write was NOT sent to NetSuite.",
                                        "blocked": True,
                                    }
                                )
                            else:
                                result_str = json.dumps(
                                    {
                                        "error": f"Record type '{record_type}' is not allowed for "
                                        f"AI-initiated {mutation_type} operations.",
                                        "blocked": True,
                                    }
                                )
                        else:
                            yield ("confirmation_required", payload.model_dump())
                            _confirmation_result: dict[str, Any] = {
                                "confirmation_required": True,
                                "mutation_type": mutation_type,
                                "record_type": record_type,
                                "message": (
                                    f"This {mutation_type} operation on {record_type} requires human "
                                    f"confirmation. The confirmation dialog has been shown to the user. "
                                    f"Do NOT proceed until the user explicitly approves."
                                ),
                            }
                            if _ask_user_rejected:
                                # A hinted name that failed verification
                                # produces NO slot — tell the model so here,
                                # on the SAME result the card is announced
                                # in, rather than silently dropping the
                                # request. The write still proceeds to the
                                # card either way (unresolved names are not a
                                # validation failure).
                                _confirmation_result["unresolved_ask_user_fields"] = _ask_user_rejected
                            result_str = json.dumps(_confirmation_result)

                        elapsed_ms = int((time.monotonic() - t0) * 1000)
                        yield (
                            "tool_end",
                            {
                                "tool_name": block.name,
                                "step": step,
                                "duration_ms": elapsed_ms,
                                "success": payload is not None,
                                "result_summary": (
                                    "Confirmation required"
                                    if payload is not None
                                    else ("Unparseable write payload" if payload_unparseable else "Blocked record type")
                                ),
                            },
                        )
                        _log_entry = build_tool_call_log_entry(
                            step=step,
                            agent_name=self.agent_name,
                            tool_name=block.name,
                            params=block.input,
                            result_str=result_str,
                            duration_ms=elapsed_ms,
                        )
                        _last_validation = getattr(self, "_last_validation", None)
                        if payload is not None and _last_validation is not None and not _last_validation.ok:
                            # The repair loop gave up (exit_reason "stall" or
                            # "budget", not "done") and this card is being shown
                            # despite a still-invalid payload. Without this, the
                            # persisted trail cannot distinguish "validated clean
                            # on the first attempt" from "failed repeatedly, we
                            # gave up, and showed a human an invalid payload
                            # anyway" — very different events on a financial
                            # write path. SSE/card behavior is unchanged; this
                            # only annotates the persisted tool_calls_log entry.
                            _log_entry["validation_failed_before_confirmation"] = _validation_failure_detail(
                                _last_validation
                            )
                        tool_calls_log.append(_log_entry)
                        tool_results_content.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_str,
                            }
                        )
                        continue
                    # ── End mutation intercept ──

                    # ── Recon group-approve HITL intercept (Phase 2) ──
                    if block.name == "recon_approve_group":
                        from app.services.chat.write_confirmation_service import (
                            build_recon_group_confirmation,
                        )

                        payload = build_recon_group_confirmation(
                            tool_input=block.input,
                            session_id=session_id if session_id else str(self.tenant_id),
                        )
                        yield ("confirmation_required", payload.model_dump())
                        result_str = json.dumps(
                            {
                                "confirmation_required": True,
                                "message": (
                                    "Approving this resolution group requires human confirmation. "
                                    "The confirmation card has been shown. Do NOT proceed until "
                                    "the user explicitly approves."
                                ),
                            }
                        )
                        elapsed_ms = int((time.monotonic() - t0) * 1000)
                        yield (
                            "tool_end",
                            {
                                "tool_name": block.name,
                                "step": step,
                                "duration_ms": elapsed_ms,
                                "success": True,
                                "result_summary": "Confirmation required",
                            },
                        )
                        tool_calls_log.append(
                            build_tool_call_log_entry(
                                step=step,
                                agent_name=self.agent_name,
                                tool_name=block.name,
                                params=block.input,
                                result_str=result_str,
                                duration_ms=elapsed_ms,
                            )
                        )
                        tool_results_content.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_str,
                            }
                        )
                        continue
                    # ── End recon group-approve intercept ──

                    policy_result = policy_evaluate(active_policy, block.name, block.input)
                    if not policy_result["allowed"]:
                        result_str = json.dumps(
                            {"error": f"Policy blocked: {policy_result.get('reason', 'Not allowed')}"}
                        )
                    else:
                        result_str = await execute_tool_call(
                            tool_name=block.name,
                            tool_input=block.input,
                            tenant_id=self.tenant_id,
                            actor_id=self.user_id,
                            correlation_id=self.correlation_id,
                            db=db,
                            context_need=getattr(self, "_context_need", None),
                            session_id=session_id,
                        )
                        if active_policy and active_policy.blocked_fields:
                            try:
                                parsed = json.loads(result_str)
                                parsed = redact_output(active_policy, parsed)
                                result_str = json.dumps(parsed, default=str)
                            except (json.JSONDecodeError, TypeError):
                                pass

                    # Capture the ORIGINAL, untruncated result BEFORE row-capping.
                    # The LLM-facing string is truncated below (token budget), but the
                    # in-turn full-payload sidecar AND the persisted
                    # ChatMessage.tool_calls[].result_payload — both of which
                    # report.compose resolves to render the FULL, "uncapped frozen
                    # payload" — must see all rows. Without this, a >500-row result
                    # silently composes a report missing rows 501..N (finding #10).
                    full_result_str = result_str
                    result_str = _truncate_tool_result(result_str)

                    raw_result_strings.append(result_str)
                    elapsed_ms = int((time.monotonic() - t0) * 1000)

                    _result_dict = {"result_summary": result_str}
                    _row_count = tool_call_row_count(_result_dict)
                    _had_error = tool_call_had_error(_result_dict)
                    _summary = (
                        f"{_row_count} rows returned"
                        if _row_count and not _had_error
                        else ("Error" if _had_error else "Done")
                    )
                    yield (
                        "tool_end",
                        {
                            "tool_name": block.name,
                            "step": step,
                            "duration_ms": elapsed_ms,
                            "success": not _had_error,
                            "result_summary": _summary,
                        },
                    )

                    # Allow orchestrator to intercept specific tool results
                    # (e.g. financial reports → SSE event + condensed LLM context)
                    llm_result_str = result_str
                    if tool_result_interceptor is not None:
                        # Pass the tool params AND the original (pre-truncation) result.
                        # The orchestrator's interceptor extracts the in-turn full-payload
                        # sidecar from the FULL string (extract_result_payload) so a
                        # same-turn report.compose resolves all rows uncapped, while the
                        # LLM-facing string (result_str) stays row-capped.
                        intercept_data, llm_result_str = _call_tool_result_interceptor(
                            tool_result_interceptor, block.name, result_str, block.input, full_result_str
                        )
                        if intercept_data is not None:
                            yield "tool_intercept", intercept_data

                    # Metric trust boundary — TOOL-enforced, not interceptor-dependent.
                    # When no interceptor is wired (e.g. the vs-MCP benchmark runner in
                    # benchmarks/agent_runner.py), llm_result_str is the raw result above
                    # and a metric_compute payload would hand its computed number straight
                    # to the LLM — the exact anti-hallucination breach the catalog exists to
                    # prevent, leaking into the north-star CI gate. Mirror the non-streaming
                    # run() guard: pass llm_result_str through the SAME suppressor so a
                    # metric's number can never reach the model on EITHER path. This is a
                    # no-op for non-metric results (opt-in via suppress_llm_value) and
                    # idempotent over an interceptor that already condensed a metric (the
                    # condensed string carries no suppress_llm_value flag).
                    llm_result_str = _suppress_metric_value_for_llm(llm_result_str)

                    tool_calls_log.append(
                        build_tool_call_log_entry(
                            step=step,
                            agent_name=self.agent_name,
                            tool_name=block.name,
                            params=block.input,
                            # Use the ORIGINAL pre-truncation result so the persisted
                            # result_payload (the CROSS-TURN report.compose fallback)
                            # is FULL/uncapped — matching the in-turn sidecar (#10).
                            result_str=full_result_str,
                            duration_ms=elapsed_ms,
                        )
                    )

                    tool_results_content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": llm_result_str,
                        }
                    )

                    # Early exit: if this tool returned data and there are more
                    # tools queued, skip redundant DATA tools — but always allow
                    # knowledge/context tools (workspace_search, rag_search, web_search)
                    remaining_blocks = response.tool_use_blocks[i + 1 :]
                    skippable = [b for b in remaining_blocks if b.name not in _KNOWLEDGE_TOOLS]
                    must_run = [b for b in remaining_blocks if b.name in _KNOWLEDGE_TOOLS]
                    if (
                        getattr(self, "_context_need", None) != "full"
                        and skippable
                        and _has_successful_data_result([result_str])
                    ):
                        print(
                            f"[AGENT] {self.agent_name} data returned, skipping "
                            f"{len(skippable)} data tools, keeping {len(must_run)} knowledge tools",
                            flush=True,
                        )
                        for skipped in skippable:
                            tool_results_content.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": skipped.id,
                                    "content": json.dumps(
                                        {
                                            "skipped": "Previous tool returned data. "
                                            "Present those results instead of running more queries."
                                        }
                                    ),
                                }
                            )
                        if not must_run:
                            break

                # Soft enforcement: nudge LLM to stop if data was already returned
                if (
                    getattr(self, "_context_need", None) != "full"
                    and step >= 1
                    and _has_successful_data_result(raw_result_strings)
                ):
                    tool_results_content.append(
                        {
                            "type": "text",
                            "text": _DATA_SUCCESS_NUDGE,
                        }
                    )

                messages.append(adapter.build_tool_result_message(tool_results_content))

            # Loop exhausted — force final response (no tools, must answer)
            print(
                f"[AGENT] {self.agent_name} streaming loop exhausted {self.max_steps} steps",
                flush=True,
            )
            messages.append(
                {
                    "role": "user",
                    "content": "You have used all available tool steps. You MUST now provide your final answer to the user based on everything you have gathered so far. Do NOT output only reasoning — give the user a clear, helpful response.",
                }
            )
            response = None
            async for event_type, payload in adapter.stream_message(
                model=model,
                max_tokens=16384,
                system=prompt_parts.static,
                system_dynamic=prompt_parts.dynamic,
                messages=messages,
                thinking_level=current_thinking_level,
            ):
                if event_type == "text":
                    yield "text", payload
                elif event_type == "response":
                    response = payload

            if response:
                total_input_tokens += response.usage.input_tokens
                total_output_tokens += response.usage.output_tokens
                total_cache_creation += response.usage.cache_creation_input_tokens
                total_cache_read += response.usage.cache_read_input_tokens

            final_text = "\n".join(response.text_blocks) if response and response.text_blocks else ""

            # Extract confidence BEFORE stripping tag so agent self-score is used
            # (Haiku fallback only fires when tag is missing)
            tools_used = [c.get("tool", "") for c in tool_calls_log]
            tool_ok = sum(1 for c in tool_calls_log if not tool_call_had_error(c))
            tool_rate = tool_ok / len(tool_calls_log) if tool_calls_log else 0.0

            assessment = await extract_structured_confidence(
                user_question=task,
                assistant_response=final_text[:500],
                tools_used=tools_used,
                tool_success_rate=tool_rate,
            )
            confidence = assessment.score
            final_text = strip_confidence_tag(final_text)
            if confidence <= 2:
                final_text += _LOW_CONFIDENCE_DISCLAIMER
            logger.info("agent.confidence agent=%s score=%d source=%s", self.agent_name, confidence, assessment.source)

            composite = _compute_confidence(confidence, context, tool_calls_log)

            await _maybe_store_query_pattern(db, self.tenant_id, task, tool_calls_log)

            yield (
                "response",
                AgentResult(
                    success=True,
                    data=final_text,
                    tool_calls_log=tool_calls_log,
                    tokens_used=TokenUsage(
                        total_input_tokens, total_output_tokens, total_cache_creation, total_cache_read
                    ),
                    agent_name=self.agent_name,
                    confidence_score=composite,
                ),
            )

        except Exception as exc:
            logger.error("Agent %s streaming failed: %s", self.agent_name, exc, exc_info=True)
            yield (
                "response",
                AgentResult(
                    success=False,
                    error=str(exc),
                    tool_calls_log=tool_calls_log,
                    tokens_used=TokenUsage(
                        total_input_tokens, total_output_tokens, total_cache_creation, total_cache_read
                    ),
                    agent_name=self.agent_name,
                ),
            )
