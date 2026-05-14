"""Prompt cache — split system prompts into static (cached) and dynamic (per-turn) parts.

The Anthropic API supports multiple system blocks with independent cache_control.
By separating the stable base prompt from per-turn XML context blocks, we get
~90% cache hit rate on the static portion across agentic loop iterations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# XML blocks that change every turn and should NOT land in the cached static
# prefix. Extracted into ``dynamic`` so the cacheable ``static`` block stays
# byte-stable across turns.
#
# Members:
#   - tenant_vernacular: per-turn NER + entity resolution
#   - domain_knowledge: RAG chunks retrieved for this turn
#   - proven_patterns: tenant query patterns matched for this turn
#   - financial_context: financial intent classification
#   - learned_rules: query-aware tenant business rules (rebuilt per-turn)
#   - current_datetime: today/now block — HH:MM changes every minute
#
# learned_rules and current_datetime were added after a cache audit found
# they were silently bleeding into the static prefix and invalidating cache
# up to once per minute. See test_prompt_cache.py.
_DYNAMIC_BLOCK_RE = re.compile(
    r"<(tenant_vernacular|domain_knowledge|proven_patterns|financial_context|learned_rules|current_datetime)>"
    r".*?"
    r"</\1>",
    re.DOTALL,
)


@dataclass(frozen=True)
class StaticDynamicPrompt:
    """A system prompt split into cached (static) and per-turn (dynamic) parts."""

    static: str  # Cached across turns
    dynamic: str  # Rebuilt every turn


def split_system_prompt(full_prompt: str) -> StaticDynamicPrompt:
    """Split a system prompt into static and dynamic parts.

    Extracts ``<tenant_vernacular>``, ``<domain_knowledge>``, ``<proven_patterns>``,
    and ``<financial_context>`` XML blocks into the dynamic part. Everything else
    stays in static.
    """
    dynamic_blocks: list[str] = []

    def _extract(match: re.Match) -> str:
        dynamic_blocks.append(match.group(0))
        return ""

    static = _DYNAMIC_BLOCK_RE.sub(_extract, full_prompt)

    # Clean up leftover blank lines where blocks were removed
    static = re.sub(r"\n{3,}", "\n\n", static).strip()

    dynamic = "\n\n".join(dynamic_blocks) if dynamic_blocks else ""

    return StaticDynamicPrompt(static=static, dynamic=dynamic)
