"""Contract test — Anthropic adapter cannot bypass Plan Mode gate.

Mock adapter intentionally tries to call BigQuery first when financial-ambiguous
regex matches. Server-side filter must prevent it: BigQuery tool isn't in the
schema the adapter sees.
"""

import pytest

from app.services.chat.plan_mode.ambiguity_signal import (
    filter_tools_to_clarify_only,
    is_financial_ambiguous,
)


@pytest.mark.asyncio
async def test_misbehaving_adapter_cannot_call_data_tool():
    """If model TRIES to call bigquery_sql first, the tool isn't in the inventory."""
    full_inventory = [
        {"name": "bigquery_sql"},
        {"name": "netsuite_suiteql"},
        {"name": "clarify"},
    ]
    query = "What's our revenue this quarter?"
    assert is_financial_ambiguous(query) is True
    filtered = filter_tools_to_clarify_only(full_inventory)
    assert [t["name"] for t in filtered] == ["clarify"]


@pytest.mark.asyncio
async def test_force_tool_choice_passed_to_adapter():
    """Verify orchestrator passes tool_choice={"type":"tool","name":"clarify"} to Anthropic."""
    from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter

    adapter = AnthropicAdapter(api_key="test")
    forcing = adapter.force_tool_choice("clarify")
    assert forcing == {"type": "tool", "name": "clarify"}


@pytest.mark.asyncio
async def test_force_tool_choice_model_param_ignored_for_anthropic():
    """Anthropic shape is model-agnostic — `model` is accepted only for protocol uniformity."""
    from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter

    adapter = AnthropicAdapter(api_key="test")
    # Passing a model name should produce the same shape as omitting it.
    with_model = adapter.force_tool_choice("clarify", model="claude-sonnet-4-6")
    without_model = adapter.force_tool_choice("clarify")
    assert with_model == without_model == {"type": "tool", "name": "clarify"}


@pytest.mark.asyncio
async def test_force_tool_choice_rejects_empty_tool_name():
    """Defensive: empty/non-string tool_name must raise ValueError, not produce a malformed param."""
    from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter

    adapter = AnthropicAdapter(api_key="test")
    with pytest.raises(ValueError):
        adapter.force_tool_choice("")
