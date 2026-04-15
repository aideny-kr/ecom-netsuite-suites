"""Base specialist agent for the multi-agent orchestration system.

Each specialist runs its own mini agentic loop (up to max_steps iterations)
using the same LLM adapter and tool execution infrastructure as the main
orchestrator, but scoped to a specific task and tool subset.
"""

from __future__ import annotations

import abc
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from app.services.chat.llm_adapter import BaseLLMAdapter, LLMResponse, TokenUsage
from app.services.chat.prompt_cache import split_system_prompt
from app.services.chat.tool_call_results import (
    build_tool_call_log_entry,
    tool_call_had_error,
    tool_call_row_count,
)
from app.services.chat.tool_categories import categorize
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
        return (
            "\n## CURRENT DATE & TIME\n"
            f"Timezone: {tz_label}. "
            f"Today: {local_today} ({local_now.strftime('%A, %B %d, %Y')}), "
            f"local time: {local_now.strftime('%H:%M')}. "
            f"'today' = TO_DATE('{local_today}', 'YYYY-MM-DD'). "
            f"'yesterday' = TO_DATE('{local_yesterday}', 'YYYY-MM-DD'). "
            f"When the user says 'last N months', anchor on the month BEFORE "
            f"today's month as the most recent complete month."
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
    required = any(
        categorize(t.get("tool_name", "")) in _DATA_CATEGORIES
        for t in tool_calls_log
    )

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
        _learned_rules = context.get("learned_rules", [])
        if _learned_rules:
            lr_block = "\n<learned_rules>\nTenant-specific business rules — FOLLOW THESE STRICTLY:\n"
            for rule in _learned_rules:
                lr_block += f"- {rule}\n"
            lr_block += "</learned_rules>"
            _system_prompt += lr_block

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

                    t0 = time.monotonic()

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
            )
            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens
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
        tool_result_interceptor: Callable[[str, str], tuple[tuple[str, dict] | None, str]] | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
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
        _learned_rules = context.get("learned_rules", [])
        if _learned_rules:
            lr_block = "\n<learned_rules>\nTenant-specific business rules — FOLLOW THESE STRICTLY:\n"
            for rule in _learned_rules:
                lr_block += f"- {rule}\n"
            lr_block += "</learned_rules>"
            _system_prompt += lr_block

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
                        intercept_data, llm_result_str = tool_result_interceptor(block.name, result_str)
                        if intercept_data is not None:
                            yield "tool_intercept", intercept_data

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
            ):
                if event_type == "text":
                    yield "text", payload
                elif event_type == "response":
                    response = payload

            if response:
                total_input_tokens += response.usage.input_tokens
                total_output_tokens += response.usage.output_tokens

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
