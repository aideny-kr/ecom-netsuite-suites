"""SpecializedAgent — composition-based agent driven by YAML config + prompt + hooks.

Uses composition over inheritance: behavior is determined by AgentYAMLConfig,
prompt text, and HookManager hooks rather than deep class hierarchies.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.services.chat.agents.agent_yaml_config import AgentYAMLConfig
from app.services.chat.agents.base_agent import BaseSpecialistAgent
from app.services.chat.agents.tool_filter import get_tools_for_agent
from app.services.chat.tools import build_local_tool_definitions


class SpecializedAgent(BaseSpecialistAgent):
    """Agent driven by YAML config, prompt text, and optional knowledge chunks."""

    def __init__(
        self,
        config: AgentYAMLConfig,
        prompt_text: str,
        knowledge: list[str],
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        correlation_id: str,
    ) -> None:
        super().__init__(
            tenant_id=tenant_id,
            user_id=user_id,
            correlation_id=correlation_id,
        )
        self._config = config
        self._prompt_text = prompt_text
        self._knowledge = knowledge

    # --- Properties delegating to config ---

    @property
    def agent_name(self) -> str:
        return self._config.agent_id

    @property
    def agent_id(self) -> str:
        return self._config.agent_id

    @property
    def display_name(self) -> str:
        return self._config.display_name

    @property
    def description(self) -> str:
        return self._config.description

    @property
    def routing_rules(self) -> list[dict[str, Any]]:
        return [r.model_dump() for r in self._config.routing_rules]

    @property
    def tool_ids(self) -> list[str]:
        return self._config.tool_ids

    @property
    def rag_partitions(self) -> list[str]:
        return self._config.rag_partitions

    @property
    def max_steps(self) -> int:
        return self._config.max_steps

    @property
    def model_preference(self) -> str | None:
        return self._config.model_preference

    @property
    def cost_budget(self) -> float | None:
        return self._config.cost_budget

    @property
    def requires_confirmation(self) -> bool:
        return self._config.requires_confirmation

    # --- Computed properties ---

    @property
    def system_prompt(self) -> str:
        from app.services.chat.agents.base_agent import build_current_date_block

        base = self._prompt_text
        if self._knowledge:
            parts = ["\n<knowledge>"]
            for chunk in self._knowledge:
                parts.append(chunk)
            parts.append("</knowledge>")
            base += "\n".join(parts)
        # Always append the current-date block so the LLM never has to guess
        # from its training cutoff. Benefits BI agent, pricing agent, recon
        # agent, and any future YAML-driven specialized agent.
        date_block = build_current_date_block(self._user_timezone)
        if date_block:
            base += date_block
        return base

    @property
    def tool_definitions(self) -> list[dict]:
        all_tools = build_local_tool_definitions()
        # Special case: unified-agent gets all tools
        if self._config.agent_id == "unified-agent":
            return all_tools
        return get_tools_for_agent(all_tools, self._config.tool_ids or None)

    # --- Protocol lifecycle hooks (default pass-through) ---

    async def pre_execute(self, task: str, context: dict[str, Any]) -> dict[str, Any]:
        return context

    async def post_tool(self, tool_name: str, tool_input: dict, tool_result: str) -> str:
        return tool_result

    async def pre_response(self, response_text: str) -> str:
        return response_text

    async def on_error(self, error: Exception, context: dict[str, Any]) -> str | None:
        return None
