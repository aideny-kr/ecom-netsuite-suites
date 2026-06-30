"""Provider-agnostic thinking-level vocabulary.

A `thinking_level` is one of none|low|med|high|xhigh. Each adapter maps it to
its native parameter (Anthropic budget_tokens; OpenAI/OpenRouter reasoning_effort).
This module owns ONLY the vocabulary so the mapping lives in one place.
"""

LEVELS: tuple[str, ...] = ("none", "low", "med", "high", "xhigh")

# Anthropic extended-thinking budget_tokens per level. 0 == thinking disabled.
_BUDGETS: dict[str, int] = {
    "none": 0,
    "low": 2048,
    "med": 6144,
    "high": 12288,
    "xhigh": 24576,
}

# OpenAI/OpenRouter reasoning_effort per level. None == omit the param.
# The OpenAI-compatible enum is only low|medium|high — there is no "xhigh", so
# our internal "xhigh" maps down to "high" (sending "xhigh" would 400).
_EFFORT: dict[str, str] = {
    "low": "low",
    "med": "medium",
    "high": "high",
    "xhigh": "high",
}

# Escalation: one step up, capped at xhigh. "low" jumps to "high" so an explicit
# escalate from a shallow base makes a meaningful difference.
_NEXT: dict[str, str] = {
    "none": "med",
    "low": "high",
    "med": "high",
    "high": "xhigh",
    "xhigh": "xhigh",
}


def budget_for(level: str | None) -> int:
    """Anthropic budget_tokens for a level (0 = thinking off)."""
    return _BUDGETS.get(level or "", 0)


def reasoning_effort(level: str | None) -> str | None:
    """OpenAI/OpenRouter reasoning_effort for a level (None = omit)."""
    return _EFFORT.get(level or "")


# Anthropic adaptive-thinking effort per level (output_config.effort). Unlike the
# OpenAI reasoning_effort enum, Anthropic supports "xhigh", so it maps through.
# (The anthropic 0.79 SDK types effort as low|medium|high|max, but output_config
# is a runtime dict so "xhigh" reaches the API, which honours it on Sonnet-5-class
# models.) None == omit (no thinking).
_ANTHROPIC_EFFORT: dict[str, str] = {
    "low": "low",
    "med": "medium",
    "high": "high",
    "xhigh": "xhigh",
}


def anthropic_effort(level: str | None) -> str | None:
    """Anthropic output_config.effort for a level (None = omit)."""
    return _ANTHROPIC_EFFORT.get(level or "")


# Models still on LEGACY extended thinking (thinking={type:enabled,budget_tokens}).
# Everything newer (Sonnet 5, Sonnet 4.6, Opus 4.6+, Fable) uses adaptive thinking
# + effort; Haiku does no thinking. budget_tokens 400s on adaptive-only models, and
# adaptive+effort errors on the legacy ones — so the adapter must branch on this.
_LEGACY_THINKING_MARKERS = (
    "sonnet-4-5",
    "opus-4-5",
    "sonnet-4-20250514",
    "opus-4-20250514",
    "opus-4-1",
)


def thinking_mode(model: str | None) -> str:
    """Classify how a model does thinking:
    'none'     — Haiku (thinking unsupported),
    'legacy'   — 4.5 / 4.0 / 4.1 (extended thinking via budget_tokens),
    'adaptive' — Sonnet 5 / 4.6 / Opus 4.6+ / Fable (adaptive thinking + effort).
    Defaults to 'adaptive' for unknown models — that's Anthropic's forward direction
    (budget_tokens is deprecated), and the model is already VALID_MODELS-gated upstream.
    """
    m = (model or "").lower()
    if "haiku" in m:
        return "none"
    if any(k in m for k in _LEGACY_THINKING_MARKERS):
        return "legacy"
    return "adaptive"


def next_level(level: str | None) -> str:
    """One escalation step up, capped at xhigh."""
    return _NEXT.get(level or "", "high")


def is_forced_tool_choice(tool_choice: dict | str | None) -> bool:
    """True when tool_choice forces a tool (type 'tool'/'any', or str 'any'/'required').

    Extended thinking is INCOMPATIBLE with a forced tool_choice — the Anthropic API
    400s. Forcing only ever happens at step 0 of the agentic loop, so a forced-tool
    turn must run thinking-OFF for the WHOLE turn: if step 0 suppresses thinking (no
    thinking block on that assistant turn) and a later step re-enabled it, that later
    request would 400 on the blockless history. Callers use this both to suppress
    thinking on the forced hop (adapter) and to pin the turn thinking-off (agent loop).
    """
    if isinstance(tool_choice, dict):
        return tool_choice.get("type") in ("tool", "any")
    if isinstance(tool_choice, str):
        return tool_choice in ("any", "required")
    return False
