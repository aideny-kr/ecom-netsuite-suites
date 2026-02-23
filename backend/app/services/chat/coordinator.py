"""Multi-agent coordinator with semantic routing engine.

Implements a supervisor pattern with a fast heuristic classifier that routes
user questions to the right specialist agent without an LLM call in most cases.
Falls back to LLM-based planning only for ambiguous queries.

Routes:
- DOCUMENTATION → Haiku RAG agent (rag_search + web_search)
- DATA_QUERY → Sonnet SuiteQL agent (with tenant_schema injection)
- WORKSPACE_DEV → Workspace IDE agent (file ops + propose_patch)
- ANALYSIS → Data analysis agent (requires prior data)
- AMBIGUOUS → LLM-based planning (fallback)
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncGenerator

from app.core.config import settings
from app.services.chat.agents import (
    AgentResult,
    DataAnalysisAgent,
    RAGAgent,
    SuiteQLAgent,
    WorkspaceAgent,
)
from app.services.chat.llm_adapter import BaseLLMAdapter, LLMResponse, TokenUsage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.netsuite_metadata import NetSuiteMetadata
    from app.models.policy_profile import PolicyProfile

logger = logging.getLogger(__name__)


# ── Intent classification ─────────────────────────────────────────────────


class IntentType(str, enum.Enum):
    """Route categories for the semantic classifier."""

    DOCUMENTATION = "documentation"
    DATA_QUERY = "data_query"
    WORKSPACE_DEV = "workspace_dev"
    ANALYSIS = "analysis"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class RouteConfig:
    """Maps an intent to the agent(s) and model to use."""

    intent: IntentType
    agents: list[str]  # Agent names in execution order
    model_override: str | None = None  # Override specialist model for this route
    parallel: bool = False


# ── Route registry (extensible — add new routes here) ─────────────────────

ROUTE_REGISTRY: dict[IntentType, RouteConfig] = {
    IntentType.DOCUMENTATION: RouteConfig(
        intent=IntentType.DOCUMENTATION,
        agents=["rag"],
        model_override=None,  # Uses default specialist model (Haiku)
    ),
    IntentType.DATA_QUERY: RouteConfig(
        intent=IntentType.DATA_QUERY,
        agents=["suiteql"],
        model_override=None,  # SuiteQL agent gets its own model from settings.MULTI_AGENT_SQL_MODEL
    ),
    IntentType.WORKSPACE_DEV: RouteConfig(
        intent=IntentType.WORKSPACE_DEV,
        agents=["workspace"],
        model_override=None,  # Uses default specialist model
    ),
    IntentType.ANALYSIS: RouteConfig(
        intent=IntentType.ANALYSIS,
        agents=["suiteql", "analysis"],  # Data first, then analysis
        parallel=False,
    ),
}


# ── Keyword patterns for fast heuristic classification ─────────────────────

# Patterns are checked in order. First match wins.
# Each entry: (IntentType, compiled_regex_pattern)

_HEURISTIC_RULES: list[tuple[IntentType, re.Pattern[str]]] = [
    # --- WORKSPACE_DEV: SuiteScript development tasks ---
    # Checked first because "write a script" is unambiguously workspace, not docs.
    (
        IntentType.WORKSPACE_DEV,
        re.compile(
            r"""(?xi)
            \b(?:
                write\s+(?:a\s+)?(?:suite)?script |
                create\s+(?:a\s+)?(?:suite)?script |
                refactor\s+(?:the\s+)?(?:suite)?script |
                review\s+(?:the\s+)?(?:change|changeset|patch|code|script|pr) |
                propose\s+(?:a\s+)?(?:change|patch) |
                jest\s+test |
                unit\s+test |
                write\s+(?:a\s+)?test |
                workspace\s+file |
                read\s+(?:the\s+)?file |
                list\s+(?:the\s+)?files |
                search\s+(?:the\s+)?(?:workspace|codebase|scripts?) |
                sdf\s+(?:validate|deploy|project) |
                user\s*event\s*script |
                scheduled\s*script |
                map\s*/?\s*reduce |
                suitelet |
                restlet |
                client\s*script |
                \.js\s*file |
                file\s*cabinet
            )\b
            """
        ),
    ),
    # --- DOCUMENTATION: How-to, syntax, error lookups ---
    (
        IntentType.DOCUMENTATION,
        re.compile(
            r"""(?xi)
            \b(?:
                how\s+(?:do|does|to|can)\s+(?:i|you|we) |
                what\s+is\s+(?:a|an|the)?\s*(?:suiteql|suitescript|netsuite|record\s+type) |
                explain\s+(?:the\s+)?(?:syntax|error|api|concept|difference) |
                documentation\s+(?:for|about|on) |
                error\s+(?:code|message)[\s:] |
                what\s+(?:does|is)\s+(?:the\s+)?(?:error|field|module) |
                netsuite\s+(?:api|help|docs|documentation|reference|guide) |
                suiteql\s+(?:syntax|reference|docs|help) |
                suitescript\s+(?:api|module|reference|docs|help) |
                n/\w+\s+module |
                what\s+(?:tables?|fields?|columns?)\s+(?:are|does|do|should) |
                governance\s+(?:limit|unit|usage)
            )\b
            """
        ),
    ),
    # --- ANALYSIS: Aggregation, trends, comparisons ---
    # Checked BEFORE DATA_QUERY so "compare sales" doesn't get caught by "sales" pattern.
    (
        IntentType.ANALYSIS,
        re.compile(
            r"""(?xi)
            \b(?:
                (?:compare|comparison)\s+ |
                (?:trend|trending)\s+(?:for|of|in|over) |
                month\s*-?\s*over\s*-?\s*month |
                year\s*-?\s*over\s*-?\s*year |
                growth\s+(?:rate|trend|in) |
                (?:analyze|analyse)\s+(?:the\s+)? |
                (?:breakdown|break\s+down)\s+(?:the\s+|of\s+)?(?:data|sales|revenue|order|transaction) |
                (?:top|bottom)\s+\d+\s+(?:customer|item|product|vendor|category)s? |
                (?:chart|graph|visuali[sz]e)\s+(?:the\s+)?(?:data|sales|revenue)
            )\b
            """
        ),
    ),
    # --- DATA_QUERY: Financial data, orders, records, lookups ---
    (
        IntentType.DATA_QUERY,
        re.compile(
            r"""(?xi)
            (?:
                \b(?:
                    show\s+(?:me\s+)?(?:the\s+)?(?:latest|recent|last|all|open|pending|total) |
                    (?:find|get|pull|fetch|look\s*up|retrieve|query)\s+ |
                    (?:latest|recent|last|open|pending|total)\s+(?:order|invoice|transaction|payment|bill|journal|customer|item|vendor) |
                    how\s+many\s+(?:order|invoice|transaction|payment|bill|customer|item|vendor)s? |
                    (?:order|invoice|transaction|payment|bill|journal|customer|item|vendor)\s+(?:number|id|status|[#]) |
                    sales\s+(?:order|total|amount|revenue|data|report) |
                    revenue\s+(?:by|for|from|today|this|last) |
                    (?:today|this\s+(?:week|month|quarter|year)|last\s+(?:week|month|quarter|year))(?:'s)?\s+(?:order|invoice|transaction|sales|revenue|payment) |
                    tell\s+me\s+about\s+(?:[#]|\bnumber\b|order|invoice|customer|transaction) |
                    (?:shopify|ecom|ecommerce)\s+(?:order|number|ref) |
                    suiteql\s+(?:query|select) |
                    run\s+(?:a\s+)?(?:query|suiteql|sql) |
                    balance\s+(?:sheet|due|outstanding) |
                    accounts?\s+(?:receivable|payable) |
                    inventory\s+(?:levels?|count|on\s*hand|available)
                )\b |
                [#]\d{4,} |
                (?:^|\s)(?:SO|INV|PO|JE|VB|RMA)\d{3,}
            )
            """
        ),
    ),
]


def classify_intent(user_message: str) -> IntentType:
    """Fast heuristic classification — no LLM call needed for clear-cut cases.

    Returns IntentType.AMBIGUOUS if no heuristic pattern matches,
    signaling the coordinator to fall back to LLM-based planning.
    """
    text = user_message.strip()

    # Short messages with just a number/ID are almost always data lookups
    if re.match(r"^#?\d{4,}$", text):
        return IntentType.DATA_QUERY

    for intent, pattern in _HEURISTIC_RULES:
        if pattern.search(text):
            return intent

    return IntentType.AMBIGUOUS


# ── Coordinator prompts ───────────────────────────────────────────────────

COORDINATOR_PLAN_PROMPT = (
    "You are a coordinator that classifies user questions and routes to specialist agents.\n"
    "LANGUAGE: Always respond in English only.\n"
    "\n"
    "Available specialists:\n"
    "- suiteql: Expert SuiteQL engineer for ANY data retrieval from NetSuite "
    "(orders, invoices, customers, items, financial data, custom records).\n"
    "- rag: Documentation/knowledge search. Use for 'how-to', error lookups, "
    "API reference, feature explanations.\n"
    "- analysis: Data interpretation — aggregations, trends, comparisons. "
    "REQUIRES data from suiteql first.\n"
    "- workspace: SuiteScript workspace operations — read/write/search files, "
    "propose code changes, review changesets.\n"
    "\n"
    "Given the user's question, output ONLY a JSON plan (no markdown, no explanation):\n"
    "{\n"
    '  "reasoning": "Brief explanation",\n'
    '  "steps": [\n'
    '    {"agent": "suiteql", "task": "Detailed task description"}\n'
    "  ],\n"
    '  "parallel": false\n'
    "}\n"
    "\n"
    "RULES:\n"
    "- Use the FEWEST agents necessary. Most questions need only 1 agent.\n"
    "- For data questions: just suiteql. For docs: just rag. For code: just workspace.\n"
    "- For complex analysis: suiteql → analysis (2 steps, sequential).\n"
    "- For data questions involving dates, include the explicit date in the task.\n"
    "- Maximum 4 steps.\n"
)

COORDINATOR_SYNTHESIS_PROMPT = (
    "You are synthesising the final answer for the user based on specialist agent results.\n"
    "\n"
    "LANGUAGE: Always respond in English only.\n"
    "\n"
    "FORMAT:\n"
    "1. Start with a direct answer to the user's question in 1-2 sentences.\n"
    "2. If agents returned data rows, present them in a **markdown table**. Include all rows.\n"
    "3. If results returned 0 rows, say so clearly and suggest possible reasons:\n"
    "   - Wrong date range? No transactions posted yet today?\n"
    "   - Different record type needed?\n"
    "   - Suggest a broader query the user could try.\n"
    "4. If agents failed or timed out, briefly explain what happened and ASK the user\n"
    "   a clarifying question to help narrow the search. For example:\n"
    "   'I wasn't able to retrieve that data. Could you tell me [specific detail]?'\n"
    "\n"
    "RULES:\n"
    "- Preserve all numeric values EXACTLY as returned — do not round or convert.\n"
    "- Do NOT fabricate data — only use what agents returned.\n"
    "- Be concise. No filler phrases or disclaimers.\n"
    "- Use column headers that are human-readable (e.g., 'Sales Channel' not 'source').\n"
    "- Format currency values with commas and 2 decimal places.\n"
    "- Do NOT show raw SQL queries, tool call JSON, or internal parameters to the user.\n"
    "  The user does not need to see the technical implementation details.\n"
    "- Do NOT echo the agent's <reasoning> blocks or internal planning text.\n"
    "- Do NOT include tool names, tool IDs, or API call details.\n"
)


# ── Data classes ───────────────────────────────────────────────────────────


@dataclass
class PlanStep:
    agent: str
    task: str


@dataclass
class CoordinatorPlan:
    reasoning: str
    steps: list[PlanStep]
    parallel: bool
    intent: IntentType = IntentType.AMBIGUOUS
    used_heuristic: bool = False  # True if classified without LLM call


@dataclass
class CoordinatorResult:
    """Final result from the multi-agent coordinator."""

    final_text: str
    citations: list[dict] = field(default_factory=list)
    tool_calls_log: list[dict] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0


# ── Coordinator class ──────────────────────────────────────────────────────


class MultiAgentCoordinator:
    """Supervisor agent with semantic routing engine."""

    def __init__(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        correlation_id: str,
        # Main model (for synthesis)
        main_adapter: BaseLLMAdapter,
        main_model: str,
        # Specialist model (cheaper/faster)
        specialist_adapter: BaseLLMAdapter,
        specialist_model: str,
        # Tenant context
        metadata: NetSuiteMetadata | None = None,
        policy: PolicyProfile | None = None,
        system_prompt: str = "",
    ) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.correlation_id = correlation_id
        self.main_adapter = main_adapter
        self.main_model = main_model
        self.specialist_adapter = specialist_adapter
        self.specialist_model = specialist_model
        self.metadata = metadata
        self.policy = policy
        self.system_prompt = system_prompt
        self.last_result: CoordinatorResult | None = None

    async def run(
        self,
        user_message: str,
        conversation_history: list[dict],
        rag_context: str = "",
    ) -> CoordinatorResult:
        """Execute the full multi-agent pipeline.

        Steps:
        1. Route — classify intent and build plan (heuristic or LLM fallback)
        2. Dispatch — run specialist agents
        3. Evaluate — check results, retry if needed
        4. Synthesise — compose final answer
        """
        all_tool_calls: list[dict] = []
        total_input = 0
        total_output = 0
        budget_remaining = settings.MULTI_AGENT_MAX_BUDGET_TOKENS

        # ── Step 1: Route ──────────────────────────────────────────────
        plan, plan_tokens = await self._route(user_message, conversation_history)
        total_input += plan_tokens.input_tokens
        total_output += plan_tokens.output_tokens
        budget_remaining -= plan_tokens.input_tokens + plan_tokens.output_tokens

        if plan is None:
            return CoordinatorResult(
                final_text="I wasn't able to plan a response. Could you rephrase your question?",
                total_input_tokens=total_input,
                total_output_tokens=total_output,
            )

        logger.info(
            "coordinator.route",
            extra={
                "tenant_id": str(self.tenant_id),
                "intent": plan.intent.value,
                "heuristic": plan.used_heuristic,
                "reasoning": plan.reasoning,
                "steps": [s.agent for s in plan.steps],
            },
        )

        # ── Step 2: Dispatch ──────────────────────────────────────────
        agent_results: list[AgentResult] = []

        for round_num in range(1 + settings.MULTI_AGENT_MAX_RETRIES):
            if budget_remaining <= 0:
                logger.warning("coordinator.budget_exhausted", extra={"budget": budget_remaining})
                break

            steps_to_run = plan.steps if round_num == 0 else self._get_retry_steps(plan, agent_results)
            if not steps_to_run:
                break

            round_results = await self._dispatch(
                steps_to_run,
                plan.parallel and round_num == 0,
                agent_results,
                budget_remaining,
            )

            for result in round_results:
                agent_results.append(result)
                all_tool_calls.extend(result.tool_calls_log)
                total_input += result.tokens_used.input_tokens
                total_output += result.tokens_used.output_tokens
                budget_remaining -= result.tokens_used.input_tokens + result.tokens_used.output_tokens

            # ── Step 3: Evaluate ──────────────────────────────────────
            if all(r.success for r in round_results):
                break  # All succeeded, proceed to synthesis

        # ── Step 4: Synthesise ────────────────────────────────────────
        final_text, synth_tokens = await self._synthesise(
            user_message, conversation_history, agent_results, rag_context
        )
        total_input += synth_tokens.input_tokens
        total_output += synth_tokens.output_tokens

        return CoordinatorResult(
            final_text=final_text,
            citations=[],
            tool_calls_log=all_tool_calls,
            total_input_tokens=total_input,
            total_output_tokens=total_output,
        )

    async def run_streaming(
        self,
        user_message: str,
        conversation_history: list[dict],
        rag_context: str = "",
    ) -> AsyncGenerator[dict, None]:
        """Like run(), but streams synthesis tokens as SSE events.

        Yields:
            {"type": "tool_status", "content": "..."} during agent dispatch
            {"type": "text", "content": "..."} during synthesis streaming
            {"type": "message", "message": {...}} as the final event
        """
        all_tool_calls: list[dict] = []
        total_input = 0
        total_output = 0
        budget_remaining = settings.MULTI_AGENT_MAX_BUDGET_TOKENS

        # ── Step 1: Route ──
        plan, plan_tokens = await self._route(user_message, conversation_history)
        total_input += plan_tokens.input_tokens
        total_output += plan_tokens.output_tokens
        budget_remaining -= plan_tokens.input_tokens + plan_tokens.output_tokens

        if plan is None:
            self.last_result = CoordinatorResult(
                final_text="I wasn't able to plan a response. Could you rephrase your question?",
                total_input_tokens=total_input,
                total_output_tokens=total_output,
            )
            yield {"type": "message", "message": {"role": "assistant", "content": self.last_result.final_text}}
            return

        logger.info(
            "coordinator.route",
            extra={
                "tenant_id": str(self.tenant_id),
                "intent": plan.intent.value,
                "heuristic": plan.used_heuristic,
                "reasoning": plan.reasoning,
                "steps": [s.agent for s in plan.steps],
            },
        )

        # ── Step 2: Dispatch (non-streaming — agents run to completion) ──
        agent_results: list[AgentResult] = []

        for step in plan.steps:
            yield {"type": "tool_status", "content": f"Running {step.agent} agent..."}

        for round_num in range(1 + settings.MULTI_AGENT_MAX_RETRIES):
            if budget_remaining <= 0:
                break

            steps_to_run = plan.steps if round_num == 0 else self._get_retry_steps(plan, agent_results)
            if not steps_to_run:
                break

            round_results = await self._dispatch(
                steps_to_run,
                plan.parallel and round_num == 0,
                agent_results,
                budget_remaining,
            )

            for result in round_results:
                agent_results.append(result)
                all_tool_calls.extend(result.tool_calls_log)
                total_input += result.tokens_used.input_tokens
                total_output += result.tokens_used.output_tokens
                budget_remaining -= result.tokens_used.input_tokens + result.tokens_used.output_tokens

            if all(r.success for r in round_results):
                break

        # ── Step 3: Streaming synthesis ──
        yield {"type": "tool_status", "content": "Composing answer..."}

        final_text_parts: list[str] = []
        synth_tokens = TokenUsage(0, 0)

        async for text_chunk, tokens in self._synthesise_streaming(
            user_message, conversation_history, agent_results, rag_context
        ):
            if text_chunk:
                final_text_parts.append(text_chunk)
                yield {"type": "text", "content": text_chunk}
            if tokens:
                synth_tokens = tokens

        total_input += synth_tokens.input_tokens
        total_output += synth_tokens.output_tokens

        final_text = "".join(final_text_parts).strip()

        self.last_result = CoordinatorResult(
            final_text=final_text,
            citations=[],
            tool_calls_log=all_tool_calls,
            total_input_tokens=total_input,
            total_output_tokens=total_output,
        )

        yield {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": final_text or "I wasn't able to find relevant information for that question. Could you rephrase or provide more details?",
                "tool_calls": all_tool_calls if all_tool_calls else None,
            },
        }

    # ── Routing ────────────────────────────────────────────────────────────

    async def _route(
        self,
        user_message: str,
        conversation_history: list[dict],
    ) -> tuple[CoordinatorPlan | None, TokenUsage]:
        """Classify intent and build an execution plan.

        Fast path: heuristic classification → direct route (no LLM call).
        Slow path: LLM-based planning for ambiguous queries.
        """
        intent = classify_intent(user_message)

        if intent != IntentType.AMBIGUOUS:
            # Fast path — build plan directly from route registry
            plan = self._build_plan_from_intent(intent, user_message)
            logger.info(
                "coordinator.heuristic_hit",
                extra={"intent": intent.value, "message_preview": user_message[:80]},
            )
            return plan, TokenUsage(0, 0)  # Zero tokens — no LLM call

        # Slow path — fall back to LLM-based planning
        logger.info(
            "coordinator.heuristic_miss",
            extra={"message_preview": user_message[:80]},
        )
        return await self._plan_with_llm(user_message, conversation_history)

    def _build_plan_from_intent(
        self,
        intent: IntentType,
        user_message: str,
    ) -> CoordinatorPlan:
        """Build a plan directly from the route registry without an LLM call."""
        route = ROUTE_REGISTRY.get(intent)
        if not route:
            # Shouldn't happen, but fall back to suiteql
            route = ROUTE_REGISTRY[IntentType.DATA_QUERY]

        steps = [PlanStep(agent=agent, task=user_message) for agent in route.agents]

        return CoordinatorPlan(
            reasoning=f"Heuristic: classified as {intent.value}",
            steps=steps,
            parallel=route.parallel,
            intent=intent,
            used_heuristic=True,
        )

    async def _plan_with_llm(
        self,
        user_message: str,
        conversation_history: list[dict],
    ) -> tuple[CoordinatorPlan | None, TokenUsage]:
        """LLM fallback for ambiguous queries — calls Haiku to produce a JSON plan."""
        from datetime import date

        today = date.today().isoformat()

        messages: list[dict] = []
        for msg in conversation_history[-6:]:
            messages.append(msg)

        messages.append(
            {
                "role": "user",
                "content": f"[Today is {today}]\nUser question: {user_message}\n\nProduce a JSON plan.",
            }
        )

        response: LLMResponse = await self.specialist_adapter.create_message(
            model=self.specialist_model,
            max_tokens=512,
            system=COORDINATOR_PLAN_PROMPT,
            messages=messages,
        )

        plan_text = "\n".join(response.text_blocks) if response.text_blocks else ""

        try:
            json_match = re.search(r"\{.*\}", plan_text, re.DOTALL)
            if json_match:
                plan_data = json.loads(json_match.group())
            else:
                plan_data = json.loads(plan_text)

            valid_agents = {"suiteql", "rag", "analysis", "workspace"}
            steps = [
                PlanStep(agent=s["agent"], task=s["task"])
                for s in plan_data.get("steps", [])
                if s.get("agent") in valid_agents
            ]

            if not steps:
                steps = [PlanStep(agent="suiteql", task=user_message)]

            return (
                CoordinatorPlan(
                    reasoning=plan_data.get("reasoning", ""),
                    steps=steps[:4],
                    parallel=plan_data.get("parallel", False),
                    intent=IntentType.AMBIGUOUS,
                    used_heuristic=False,
                ),
                response.usage,
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("coordinator.plan_parse_failed: %s", exc)
            return (
                CoordinatorPlan(
                    reasoning="Plan parsing failed, falling back to direct query",
                    steps=[PlanStep(agent="suiteql", task=user_message)],
                    parallel=False,
                    intent=IntentType.AMBIGUOUS,
                    used_heuristic=False,
                ),
                response.usage,
            )

    # ── Dispatch ───────────────────────────────────────────────────────────

    async def _dispatch(
        self,
        steps: list[PlanStep],
        parallel: bool,
        prior_results: list[AgentResult],
        budget_remaining: int,
    ) -> list[AgentResult]:
        """Instantiate and run specialist agents for the given steps."""
        if budget_remaining <= 0:
            return []

        context: dict[str, Any] = {}
        if prior_results:
            context["prior_results"] = [
                {"agent": r.agent_name, "success": r.success, "data": r.data, "error": r.error}
                for r in prior_results
            ]

        async def run_step(step: PlanStep) -> AgentResult:
            agent = self._create_agent(step.agent)
            if agent is None:
                return AgentResult(
                    success=False,
                    error=f"Unknown agent type: {step.agent}",
                    agent_name=step.agent,
                )
            # SuiteQL agent uses a stronger model for SQL reasoning
            step_model = self.specialist_model
            if step.agent == "suiteql":
                if settings.MULTI_AGENT_SQL_MODEL:
                    step_model = settings.MULTI_AGENT_SQL_MODEL
                
                # Tenant-Aware Entity Resolution via Fast NER & pg_trgm
                from app.services.chat.tenant_resolver import TenantEntityResolver
                vernacular = await TenantEntityResolver.resolve_entities(
                    user_message=step.task,
                    tenant_id=self.tenant_id,
                    db=self.db,
                    adapter=self.specialist_adapter,
                    model=self.specialist_model,
                )
                if vernacular:
                    context["tenant_vernacular"] = vernacular
                    logger.info(
                        "coordinator.tenant_vernacular_injected",
                        extra={"vernacular_len": len(vernacular), "preview": vernacular[:300]},
                    )
                    print(f"[COORDINATOR] Vernacular injected ({len(vernacular)} chars)", flush=True)
                    
            return await agent.run(
                task=step.task,
                context=context,
                db=self.db,
                adapter=self.specialist_adapter,
                model=step_model,
            )

        if parallel and len(steps) > 1:
            results = await asyncio.gather(
                *(run_step(step) for step in steps),
                return_exceptions=False,
            )
            return list(results)
        else:
            results = []
            for step in steps:
                result = await run_step(step)
                results.append(result)
                if result.success and result.data:
                    context.setdefault("prior_results", []).append(
                        {
                            "agent": result.agent_name,
                            "success": result.success,
                            "data": result.data,
                        }
                    )
            return results

    def _create_agent(self, agent_type: str) -> Any:
        """Factory for specialist agents."""
        if agent_type == "suiteql":
            return SuiteQLAgent(
                tenant_id=self.tenant_id,
                user_id=self.user_id,
                correlation_id=self.correlation_id,
                metadata=self.metadata,
                policy=self.policy,
            )
        elif agent_type == "rag":
            return RAGAgent(
                tenant_id=self.tenant_id,
                user_id=self.user_id,
                correlation_id=self.correlation_id,
            )
        elif agent_type == "analysis":
            return DataAnalysisAgent(
                tenant_id=self.tenant_id,
                user_id=self.user_id,
                correlation_id=self.correlation_id,
            )
        elif agent_type == "workspace":
            return WorkspaceAgent(
                tenant_id=self.tenant_id,
                user_id=self.user_id,
                correlation_id=self.correlation_id,
            )
        return None

    def _get_retry_steps(
        self,
        plan: CoordinatorPlan,
        results: list[AgentResult],
    ) -> list[PlanStep]:
        """Determine retry steps based on failed agents."""
        failed = [r for r in results if not r.success]
        if not failed:
            return []

        retry_steps = []
        for result in failed:
            if result.agent_name == "suiteql":
                error_context = result.error or "unknown error"
                retry_steps.append(
                    PlanStep(
                        agent="rag",
                        task=f"Look up correct field names for NetSuite query that failed with: {error_context[:200]}",
                    )
                )
                original_task = next(
                    (s.task for s in plan.steps if s.agent == "suiteql"),
                    "Retry the original query",
                )
                retry_steps.append(
                    PlanStep(
                        agent="suiteql",
                        task=f"{original_task} (Retry: use corrected field names from RAG results)",
                    )
                )
            elif result.agent_name == "rag":
                retry_steps.append(
                    PlanStep(
                        agent="rag",
                        task=f"Retry search with broader terms. Previous search failed: {result.error or 'no results'}",
                    )
                )

        return retry_steps[:3]

    # ── Synthesis ──────────────────────────────────────────────────────────

    @staticmethod
    def _sanitize_agent_data(data: str) -> str:
        """Strip internal reasoning blocks and tool call JSON from agent output.

        Keeps only the user-facing content (markdown tables, result summaries).
        """
        if not data:
            return ""
        # Remove <reasoning>...</reasoning> blocks
        cleaned = re.sub(r"<reasoning>.*?</reasoning>", "", data, flags=re.DOTALL)
        # Remove ```sql code blocks (raw queries)
        cleaned = re.sub(r"```sql\s*.*?```", "", cleaned, flags=re.DOTALL)
        # Remove raw JSON tool call blocks ({"sqlQuery":...})
        cleaned = re.sub(r'\{"sqlQuery"\s*:.*?\}', "", cleaned, flags=re.DOTALL)
        # Collapse excessive whitespace
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()[:8000]

    async def _synthesise(
        self,
        user_message: str,
        conversation_history: list[dict],
        agent_results: list[AgentResult],
        rag_context: str = "",
    ) -> tuple[str, TokenUsage]:
        """Final LLM call to compose the user-facing answer."""
        results_parts = []
        for result in agent_results:
            status = "SUCCESS" if result.success else "FAILED"
            data_preview = self._sanitize_agent_data(str(result.data)) if result.data else ""
            error_info = f" Error: {result.error}" if result.error else ""
            results_parts.append(f"[Agent: {result.agent_name}] [{status}]{error_info}\n{data_preview}")

        results_block = "\n\n---\n\n".join(results_parts)

        rag_block = ""
        if rag_context:
            rag_block = f"\n\n<background_context>\n{rag_context}\n</background_context>"

        messages: list[dict] = []
        for msg in conversation_history[-6:]:
            messages.append(msg)

        messages.append(
            {
                "role": "user",
                "content": (
                    f"User question: {user_message}\n\n"
                    f"<agent_results>\n{results_block}\n</agent_results>"
                    f"{rag_block}\n\n"
                    f"Synthesise a clear, complete answer for the user."
                ),
            }
        )

        synthesis_prompt = f"{self.system_prompt}\n\n{COORDINATOR_SYNTHESIS_PROMPT}"

        response: LLMResponse = await self.main_adapter.create_message(
            model=self.main_model,
            max_tokens=4096,
            system=synthesis_prompt,
            messages=messages,
        )

        final_text = "\n".join(response.text_blocks) if response.text_blocks else ""
        return final_text, response.usage

    async def _synthesise_streaming(
        self,
        user_message: str,
        conversation_history: list[dict],
        agent_results: list[AgentResult],
        rag_context: str = "",
    ) -> AsyncGenerator[tuple[str | None, TokenUsage | None], None]:
        """Stream synthesis tokens via the adapter's stream_message().

        Yields (text_chunk, None) for each token, then (None, TokenUsage) at end.
        """
        results_parts = []
        for result in agent_results:
            status = "SUCCESS" if result.success else "FAILED"
            data_preview = self._sanitize_agent_data(str(result.data)) if result.data else ""
            error_info = f" Error: {result.error}" if result.error else ""
            results_parts.append(f"[Agent: {result.agent_name}] [{status}]{error_info}\n{data_preview}")

        results_block = "\n\n---\n\n".join(results_parts)

        rag_block = ""
        if rag_context:
            rag_block = f"\n\n<background_context>\n{rag_context}\n</background_context>"

        messages: list[dict] = []
        for msg in conversation_history[-6:]:
            messages.append(msg)

        messages.append(
            {
                "role": "user",
                "content": (
                    f"User question: {user_message}\n\n"
                    f"<agent_results>\n{results_block}\n</agent_results>"
                    f"{rag_block}\n\n"
                    f"Synthesise a clear, complete answer for the user."
                ),
            }
        )

        synthesis_prompt = f"{self.system_prompt}\n\n{COORDINATOR_SYNTHESIS_PROMPT}"

        async for event_type, payload in self.main_adapter.stream_message(
            model=self.main_model,
            max_tokens=4096,
            system=synthesis_prompt,
            messages=messages,
        ):
            if event_type == "text":
                yield payload, None
            elif event_type == "response":
                yield None, payload.usage
