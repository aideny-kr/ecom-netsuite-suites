"""Provider-agnostic thinking-level vocabulary.

A `thinking_level` is one of none|low|med|high|xhigh. Each adapter maps it to
its native parameter (Anthropic budget_tokens; OpenAI/OpenRouter reasoning_effort).
This module owns ONLY the vocabulary so the mapping lives in one place.
"""

ThinkingLevel = str  # one of LEVELS

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
_EFFORT: dict[str, str] = {
    "low": "low",
    "med": "medium",
    "high": "high",
    "xhigh": "xhigh",
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


def next_level(level: str | None) -> str:
    """One escalation step up, capped at xhigh."""
    return _NEXT.get(level or "", "high")


# Model id prefixes considered China-origin for residency purposes.
_CHINA_ORIGIN_PREFIXES = ("z-ai/", "glm-", "deepseek", "qwen", "moonshot", "kimi")


def _is_china_origin(model: str | None) -> bool:
    m = (model or "").lower()
    return any(m.startswith(p) or p in m for p in _CHINA_ORIGIN_PREFIXES)


def resolve_escalation_target(
    *,
    tenant_model: str,
    tenant_provider: str,
    configured_model: str,
    configured_provider: str,
    flag_enabled: bool,
    allow_china_origin: bool,
    is_customer_data: bool,
) -> tuple[str, str]:
    """Pick (model, provider) for an escalated turn.

    Returns the tenant's own model/provider (native fallback) UNLESS a thinking
    model is configured, the flag is on, and — for China-origin models on a
    customer-data turn — the hard residency guard is explicitly set.
    """
    native = (tenant_model, tenant_provider)
    if not flag_enabled or not configured_model:
        return native
    if is_customer_data and _is_china_origin(configured_model) and not allow_china_origin:
        return native  # PHYSICAL BLOCK
    return (configured_model, configured_provider)
