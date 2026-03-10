"""Prompt cache — split system prompts into static (cached) and dynamic (per-turn) parts.

The Anthropic API supports multiple system blocks with independent cache_control.
By separating the stable base prompt from per-turn XML context blocks, we get
~90% cache hit rate on the static portion across agentic loop iterations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# XML blocks that change every turn and should NOT be cached
_DYNAMIC_BLOCK_RE = re.compile(
    r"<(tenant_vernacular|domain_knowledge|proven_patterns|financial_context)>"
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
