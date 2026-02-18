"""Multi-agent coordinator for the chat pipeline.

Implements a supervisor pattern: the Coordinator decomposes the user's
question into sub-tasks, delegates to specialist agents, collects and
evaluates results, retries on failure, and synthesises the final answer.

The coordinator uses the tenant's main model (Sonnet/Opus) for planning
and synthesis, while specialist agents use a cheaper/faster model (Haiku).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.services.chat.agents import (
    AgentResult,
    DataAnalysisAgent,
    RAGAgent,
    SuiteQLAgent,
)
from app.services.chat.llm_adapter import BaseLLMAdapter, LLMResponse, TokenUsage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.netsuite_metadata import NetSuiteMetadata
    from app.models.policy_profile import PolicyProfile

logger = logging.getLogger(__name__)


# ── Coordinator system prompt ──────────────────────────────────────────────

COORDINATOR_PLAN_PROMPT = (
    "You are a coordinator that decomposes user questions and delegates to specialist agents.\n"
    "\n"
    "Available specialists:\n"
    "- suiteql: Constructs and executes SuiteQL queries against NetSuite. Use for ANY "
    "data retrieval from NetSuite (transactions, invoices, customers, items, vendors, etc.).\n"
    "- rag: Searches documentation and knowledge base. Use for 'how-to' questions, "
    "feature explanations, field name lookups, or when you need reference information.\n"
    "- analysis: Analyses and interprets data. Use when query results need aggregation, "
    "comparison, trend analysis, or interpretation. REQUIRES data from another agent first.\n"
    "\n"
    "Given the user's question, output ONLY a JSON plan (no markdown, no explanation):\n"
    '{\n'
    '  "reasoning": "Brief explanation of your approach",\n'
    '  "steps": [\n'
    '    {"agent": "rag", "task": "Find which custom field stores warranty info on sales orders"},\n'
    '    {"agent": "suiteql", "task": "Query recent sales orders showing warranty field"}\n'
    '  ],\n'
    '  "parallel": false\n'
    '}\n'
    "\n"
    "RULES:\n"
    "- Use the FEWEST agents necessary.\n"
    "- Set parallel=true ONLY when steps are truly independent.\n"
    "- If the question is simple enough for a single agent, use just one step.\n"
    "- For data questions: typically suiteql alone (it has metadata in its prompt).\n"
    "- For complex data questions: rag (field lookup) → suiteql (query) → analysis (interpret).\n"
    "- For documentation questions: just rag.\n"
    "- The analysis agent MUST come after data-producing agents (suiteql/rag).\n"
    "- Maximum 4 steps.\n"
    "- Each task description must be specific and self-contained.\n"
)

COORDINATOR_SYNTHESIS_PROMPT = (
    "You are synthesising the final answer for the user based on specialist agent results.\n"
    "\n"
    "RULES:\n"
    "- Combine the outputs from all agents into a single, coherent response.\n"
    "- If agents produced data tables, present them clearly in markdown.\n"
    "- Cite tool results using [tool: tool_name] notation.\n"
    "- If any agent failed, mention what couldn't be retrieved and why.\n"
    "- Be concise and focused on what the user asked.\n"
    "- Do NOT fabricate data — only use what agents returned.\n"
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
    """Supervisor agent that orchestrates specialist agents."""

    def __init__(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        correlation_id: str,
        # Main model (for planning + synthesis)
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

    async def run(
        self,
        user_message: str,
        conversation_history: list[dict],
        rag_context: str = "",
    ) -> CoordinatorResult:
        """Execute the full multi-agent pipeline.

        Steps:
        1. Plan — decompose user question into sub-tasks
        2. Dispatch — run specialist agents
        3. Evaluate — check results, retry if needed
        4. Synthesise — compose final answer
        """
        all_tool_calls: list[dict] = []
        total_input = 0
        total_output = 0
        budget_remaining = settings.MULTI_AGENT_MAX_BUDGET_TOKENS

        # ── Step 1: Plan ──────────────────────────────────────────────
        plan, plan_tokens = await self._plan(user_message, conversation_history)
        total_input += plan_tokens.input_tokens
        total_output += plan_tokens.output_tokens
        budget_remaining -= (plan_tokens.input_tokens + plan_tokens.output_tokens)

        if plan is None:
            # Planning failed — fall back to a simple answer
            return CoordinatorResult(
                final_text="I wasn't able to plan a response. Could you rephrase your question?",
                total_input_tokens=total_input,
                total_output_tokens=total_output,
            )

        logger.info(
            "coordinator.plan",
            tenant_id=str(self.tenant_id),
            reasoning=plan.reasoning,
            steps=[s.agent for s in plan.steps],
            parallel=plan.parallel,
        )

        # ── Step 2: Dispatch ──────────────────────────────────────────
        agent_results: list[AgentResult] = []
        retry_count = 0

        for round_num in range(1 + settings.MULTI_AGENT_MAX_RETRIES):
            if budget_remaining <= 0:
                logger.warning("coordinator.budget_exhausted", budget=budget_remaining)
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
                budget_remaining -= (result.tokens_used.input_tokens + result.tokens_used.output_tokens)

            # ── Step 3: Evaluate ──────────────────────────────────────
            if all(r.success for r in round_results):
                break  # All succeeded, proceed to synthesis
            retry_count += 1

        # ── Step 4: Synthesise ────────────────────────────────────────
        final_text, synth_tokens = await self._synthesise(
            user_message, conversation_history, agent_results, rag_context
        )
        total_input += synth_tokens.input_tokens
        total_output += synth_tokens.output_tokens

        return CoordinatorResult(
            final_text=final_text,
            citations=[],  # Citations extracted from agent results
            tool_calls_log=all_tool_calls,
            total_input_tokens=total_input,
            total_output_tokens=total_output,
        )

    # ── Internal methods ──────────────────────────────────────────────────

    async def _plan(
        self,
        user_message: str,
        conversation_history: list[dict],
    ) -> tuple[CoordinatorPlan | None, TokenUsage]:
        """Call the main model to produce a plan."""
        # Build messages with recent conversation context
        messages: list[dict] = []
        for msg in conversation_history[-6:]:  # Last 3 turns for context
            messages.append(msg)

        messages.append({
            "role": "user",
            "content": f"User question: {user_message}\n\nProduce a JSON plan.",
        })

        response: LLMResponse = await self.main_adapter.create_message(
            model=self.main_model,
            max_tokens=512,
            system=COORDINATOR_PLAN_PROMPT,
            messages=messages,
        )

        plan_text = "\n".join(response.text_blocks) if response.text_blocks else ""

        # Parse JSON from response
        try:
            # Extract JSON from potential markdown code blocks
            json_match = re.search(r'\{.*\}', plan_text, re.DOTALL)
            if json_match:
                plan_data = json.loads(json_match.group())
            else:
                plan_data = json.loads(plan_text)

            steps = [
                PlanStep(agent=s["agent"], task=s["task"])
                for s in plan_data.get("steps", [])
                if s.get("agent") in ("suiteql", "rag", "analysis")
            ]

            if not steps:
                # Default: single suiteql query
                steps = [PlanStep(agent="suiteql", task=user_message)]

            return (
                CoordinatorPlan(
                    reasoning=plan_data.get("reasoning", ""),
                    steps=steps[:4],  # Cap at 4 steps
                    parallel=plan_data.get("parallel", False),
                ),
                response.usage,
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("coordinator.plan_parse_failed: %s", exc)
            # Fallback: single suiteql step
            return (
                CoordinatorPlan(
                    reasoning="Plan parsing failed, falling back to direct query",
                    steps=[PlanStep(agent="suiteql", task=user_message)],
                    parallel=False,
                ),
                response.usage,
            )

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

        # Build context from prior results
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
            return await agent.run(
                task=step.task,
                context=context,
                db=self.db,
                adapter=self.specialist_adapter,
                model=self.specialist_model,
            )

        if parallel and len(steps) > 1:
            results = await asyncio.gather(
                *(run_step(step) for step in steps),
                return_exceptions=False,
            )
            return list(results)
        else:
            # Sequential: each step can see previous step results
            results = []
            for step in steps:
                result = await run_step(step)
                results.append(result)
                # Update context for next step
                if result.success and result.data:
                    context.setdefault("prior_results", []).append({
                        "agent": result.agent_name,
                        "success": result.success,
                        "data": result.data,
                    })
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
        return None

    def _get_retry_steps(
        self,
        plan: CoordinatorPlan,
        results: list[AgentResult],
    ) -> list[PlanStep]:
        """Determine retry steps based on failed agents.

        Strategy:
        - If suiteql failed, try rag first (to look up correct fields), then suiteql again.
        - If rag failed, retry rag with broader query.
        - analysis failures are not retried (they just need better input data).
        """
        failed = [r for r in results if not r.success]
        if not failed:
            return []

        retry_steps = []
        for result in failed:
            if result.agent_name == "suiteql":
                # Ask RAG to look up the field names, then retry suiteql
                error_context = result.error or "unknown error"
                retry_steps.append(PlanStep(
                    agent="rag",
                    task=f"Look up correct field names for NetSuite query that failed with: {error_context[:200]}",
                ))
                # Find the original suiteql task
                original_task = next(
                    (s.task for s in plan.steps if s.agent == "suiteql"),
                    "Retry the original query",
                )
                retry_steps.append(PlanStep(
                    agent="suiteql",
                    task=f"{original_task} (Retry: use corrected field names from RAG results)",
                ))
            elif result.agent_name == "rag":
                retry_steps.append(PlanStep(
                    agent="rag",
                    task=f"Retry search with broader terms. Previous search failed: {result.error or 'no results'}",
                ))

        return retry_steps[:3]  # Cap retry steps

    async def _synthesise(
        self,
        user_message: str,
        conversation_history: list[dict],
        agent_results: list[AgentResult],
        rag_context: str = "",
    ) -> tuple[str, TokenUsage]:
        """Final LLM call to compose the user-facing answer."""
        # Build results summary for the synthesiser
        results_parts = []
        for result in agent_results:
            status = "SUCCESS" if result.success else "FAILED"
            data_preview = str(result.data)[:3000] if result.data else ""
            error_info = f" Error: {result.error}" if result.error else ""
            results_parts.append(
                f"[Agent: {result.agent_name}] [{status}]{error_info}\n{data_preview}"
            )

        results_block = "\n\n---\n\n".join(results_parts)

        # Include any upfront RAG context
        rag_block = ""
        if rag_context:
            rag_block = f"\n\n<background_context>\n{rag_context}\n</background_context>"

        messages: list[dict] = []
        for msg in conversation_history[-6:]:
            messages.append(msg)

        messages.append({
            "role": "user",
            "content": (
                f"User question: {user_message}\n\n"
                f"<agent_results>\n{results_block}\n</agent_results>"
                f"{rag_block}\n\n"
                f"Synthesise a clear, complete answer for the user."
            ),
        })

        # Use tenant's main model + system prompt for synthesis
        synthesis_prompt = f"{self.system_prompt}\n\n{COORDINATOR_SYNTHESIS_PROMPT}"

        response: LLMResponse = await self.main_adapter.create_message(
            model=self.main_model,
            max_tokens=4096,
            system=synthesis_prompt,
            messages=messages,
        )

        final_text = "\n".join(response.text_blocks) if response.text_blocks else ""
        return final_text, response.usage
