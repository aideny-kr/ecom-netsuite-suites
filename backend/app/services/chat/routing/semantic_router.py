"""Tier 2: Semantic routing via Haiku classification (~50ms).

Builds a classification prompt listing all available agents and their
descriptions, sends it to Haiku, and parses the agent_id from the response.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.chat.agents.agent_yaml_config import AgentYAMLConfig
    from app.services.chat.llm_adapter import BaseLLMAdapter

logger = logging.getLogger(__name__)

_FALLBACK = "unified-agent"


class SemanticRouter:
    """LLM-based semantic agent classifier."""

    def __init__(self) -> None:
        pass

    async def route(
        self,
        query: str,
        available_agents: list[AgentYAMLConfig],
        adapter: BaseLLMAdapter,
    ) -> str:
        """Classify query to an agent_id using Haiku.

        Returns:
            An agent_id string, or "unified-agent" as fallback.
        """
        valid_ids = {a.agent_id for a in available_agents}
        valid_ids.add(_FALLBACK)

        # Build classification prompt
        agent_lines = []
        for agent in available_agents:
            agent_lines.append(f"- {agent.agent_id}: {agent.description}")
        agent_lines.append(f"- {_FALLBACK}: General-purpose catch-all agent")

        system_prompt = (
            "You are a query classifier. Given a user query, respond with ONLY the "
            "agent_id that best handles it. Choose from:\n"
            + "\n".join(agent_lines)
            + "\n\nRespond with only the agent_id, nothing else."
        )

        try:
            response = await adapter.create_message(
                model="claude-haiku-4-5-20251001",
                max_tokens=50,
                system=system_prompt,
                messages=[{"role": "user", "content": query}],
            )
            raw = response.text_blocks[0].strip() if response.text_blocks else ""
            if raw in valid_ids:
                return raw
            logger.warning("Semantic router got invalid agent_id: %s", raw)
            return _FALLBACK
        except (TimeoutError, Exception):
            logger.warning("Semantic router failed, falling back to unified-agent")
            return _FALLBACK
