"""Base specialist agent for the multi-agent orchestration system.

Each specialist runs its own mini agentic loop (up to max_steps iterations)
using the same LLM adapter and tool execution infrastructure as the main
orchestrator, but scoped to a specific task and tool subset.
"""

from __future__ import annotations

import abc
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.services.chat.llm_adapter import BaseLLMAdapter, LLMResponse, TokenUsage

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


async def _resolve_default_workspace(
    db: "AsyncSession", tenant_id: "uuid.UUID",
) -> str | None:
    """Find the most recent active workspace for a tenant."""
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


@dataclass
class AgentResult:
    """Result from a specialist agent run."""

    success: bool
    data: Any = None  # Final text output or structured data
    error: str | None = None
    tool_calls_log: list[dict] = field(default_factory=list)
    tokens_used: TokenUsage = field(default_factory=TokenUsage)
    agent_name: str = ""


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

        tool_calls_log: list[dict] = []
        total_input_tokens = 0
        total_output_tokens = 0

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

        try:
            for step in range(self.max_steps):
                response: LLMResponse = await adapter.create_message(
                    model=model,
                    max_tokens=16384,
                    system=self.system_prompt,
                    messages=messages,
                    tools=tools,
                )
                total_input_tokens += response.usage.input_tokens
                total_output_tokens += response.usage.output_tokens

                # Pure text response — agent is done
                if not response.tool_use_blocks:
                    final_text = "\n".join(response.text_blocks) if response.text_blocks else ""
                    return AgentResult(
                        success=True,
                        data=final_text,
                        tool_calls_log=tool_calls_log,
                        tokens_used=TokenUsage(total_input_tokens, total_output_tokens),
                        agent_name=self.agent_name,
                    )

                # Process tool calls
                messages.append(adapter.build_assistant_message(response))

                tool_results_content = []
                for block in response.tool_use_blocks:
                    # Auto-inject workspace_id for workspace tools
                    if block.name.startswith("workspace_"):
                        ws_id = block.input.get("workspace_id", "")
                        if not ws_id or not _is_valid_uuid(ws_id):
                            resolved = await _resolve_default_workspace(db, self.tenant_id)
                            if resolved:
                                block.input["workspace_id"] = resolved

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
                        )

                        # Output redaction
                        if active_policy and active_policy.blocked_fields:
                            try:
                                parsed = json.loads(result_str)
                                parsed = redact_output(active_policy, parsed)
                                result_str = json.dumps(parsed, default=str)
                            except (json.JSONDecodeError, TypeError):
                                pass

                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    tool_calls_log.append(
                        {
                            "step": step,
                            "agent": self.agent_name,
                            "tool": block.name,
                            "params": block.input,
                            "result_summary": result_str[:500],
                            "duration_ms": elapsed_ms,
                        }
                    )

                    tool_results_content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_str,
                        }
                    )

                messages.append(adapter.build_tool_result_message(tool_results_content))

            # Loop exhausted — make one final call without tools
            logger.warning(
                "Agent %s loop exhausted %d steps, forcing final response",
                self.agent_name,
                self.max_steps,
            )
            response = await adapter.create_message(
                model=model,
                max_tokens=16384,
                system=self.system_prompt,
                messages=messages,
            )
            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens
            final_text = "\n".join(response.text_blocks) if response.text_blocks else ""

            return AgentResult(
                success=True,
                data=final_text,
                tool_calls_log=tool_calls_log,
                tokens_used=TokenUsage(total_input_tokens, total_output_tokens),
                agent_name=self.agent_name,
            )

        except Exception as exc:
            logger.error("Agent %s failed: %s", self.agent_name, exc, exc_info=True)
            return AgentResult(
                success=False,
                error=str(exc),
                tool_calls_log=tool_calls_log,
                tokens_used=TokenUsage(total_input_tokens, total_output_tokens),
                agent_name=self.agent_name,
            )
