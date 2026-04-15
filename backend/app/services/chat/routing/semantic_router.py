"""Tier 2: Semantic routing via Haiku classification (~50ms)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.chat.agents.agent_yaml_config import AgentYAMLConfig
    from app.services.chat.llm_adapter import BaseLLMAdapter

logger = logging.getLogger(__name__)

_FALLBACK = "unified-agent"
_HISTORY_MESSAGES_TO_INCLUDE = 3  # last 3 = enough for continuity without cost


class SemanticRouter:
    """LLM-based semantic agent classifier."""

    def __init__(self) -> None:
        pass

    async def route(
        self,
        query: str,
        available_agents: list[AgentYAMLConfig],
        adapter: BaseLLMAdapter,
        history: list[dict] | None = None,
    ) -> str:
        """Classify query to an agent_id using Haiku.

        When `history` is provided, the last few messages are included in
        the classifier's context so follow-up turns like "go ahead with
        step 1" inherit the intent of the previous turn.
        """
        valid_ids = {a.agent_id for a in available_agents}
        valid_ids.add(_FALLBACK)

        agent_lines = [f"- {a.agent_id}: {a.description}" for a in available_agents]
        agent_lines.append(f"- {_FALLBACK}: General-purpose catch-all agent")

        system_sections = [
            "You are a query classifier. Pick the agent_id that best handles "
            "the user's CURRENT query, considering the conversation so far. "
            "Choose from:",
            "\n".join(agent_lines),
        ]

        if history:
            recent = history[-_HISTORY_MESSAGES_TO_INCLUDE:]
            formatted = "\n".join(
                f"{m.get('role', 'user').upper()}: {str(m.get('content', ''))[:500]}"
                for m in recent
            )
            system_sections.append(f"\nRecent conversation:\n{formatted}")

        system_sections.append("\nRespond with only the agent_id, nothing else.")
        system_prompt = "\n\n".join(system_sections)

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
        except Exception:
            logger.warning("Semantic router failed, falling back to unified-agent")
            return _FALLBACK
