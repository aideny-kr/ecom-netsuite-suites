"""Shared constants and utilities for the benchmark harness.

Extracted from baseline_runner.py and agent_runner.py to avoid
duplication. Both runners import from here.
"""

from __future__ import annotations

import uuid
from typing import Any

# Sentinel actor/correlation for benchmark-originated tool calls.
# Audit logs can filter benchmark traffic using these.
BENCHMARK_ACTOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
BENCHMARK_CORRELATION_ID = "benchmark-baseline"

# Max chars for tool result previews stored in benchmark results.
TOOL_RESULT_PREVIEW_CHARS = 1500

# Pricing per million tokens (USD). Keep in sync with Anthropic's
# published rates. Unknown models fall back to sonnet pricing.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # (input_cost_per_mtok, output_cost_per_mtok)
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5-20250929": (3.0, 15.0),
    "claude-opus-4-6": (15.0, 75.0),
    "claude-opus-4-5-20251101": (15.0, 75.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}
_DEFAULT_PRICING = (3.0, 15.0)  # sonnet fallback


def calculate_cost(*, model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate USD cost from token counts and model pricing."""
    input_rate, output_rate = MODEL_PRICING.get(model, _DEFAULT_PRICING)
    return round((input_tokens * input_rate + output_tokens * output_rate) / 1_000_000, 6)


def truncate_preview(value: Any, max_chars: int = TOOL_RESULT_PREVIEW_CHARS) -> str:
    """Truncate a value to max_chars for storage in benchmark results."""
    s = str(value) if not isinstance(value, str) else value
    return s[:max_chars] if len(s) > max_chars else s
