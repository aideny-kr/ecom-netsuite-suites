"""Tier 1: Rule-based regex routing (<1ms).

Compiles regex patterns from each enabled agent's routing_rules and
matches them against the query. Single match returns agent_id;
ambiguous same-priority matches escalate to Tier 2.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.chat.agents.agent_yaml_config import AgentYAMLConfig


class RuleRouter:
    """Fast regex-based agent router."""

    def __init__(self, agents: list[tuple[AgentYAMLConfig, bool]]) -> None:
        """Initialize with list of (config, is_enabled) tuples.

        Only enabled agents with routing_rules are compiled.
        """
        self._compiled: list[tuple[str, list[re.Pattern], int]] = []
        for config, enabled in agents:
            if not enabled:
                continue
            for rule in config.routing_rules:
                self._compiled.append(
                    (config.agent_id, re.compile(rule.pattern), rule.priority)
                )

    def route(self, query: str) -> str | None:
        """Match query against compiled patterns.

        Returns:
            agent_id if a single unambiguous match, else None.
        """
        matches: list[tuple[str, int]] = []
        for agent_id, pattern, priority in self._compiled:
            if pattern.search(query):
                # Deduplicate: only keep highest-priority match per agent
                existing = next((i for i, (aid, _) in enumerate(matches) if aid == agent_id), None)
                if existing is not None:
                    if priority > matches[existing][1]:
                        matches[existing] = (agent_id, priority)
                else:
                    matches.append((agent_id, priority))

        if not matches:
            return None

        if len(matches) == 1:
            return matches[0][0]

        # Multiple agents matched — check if priorities differ
        max_priority = max(pri for _, pri in matches)
        top_matches = [aid for aid, pri in matches if pri == max_priority]

        if len(top_matches) == 1:
            return top_matches[0]

        # Same priority, multiple agents — ambiguous, escalate
        return None
