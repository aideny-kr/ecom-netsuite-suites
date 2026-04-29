"""Contract test — OpenAI adapter cannot bypass Plan Mode gate.

Mock adapter intentionally tries to call BigQuery first when financial-ambiguous
regex matches. Server-side filter must prevent it: BigQuery tool isn't in the
schema the adapter sees.
"""

import pytest

from app.services.chat.plan_mode.ambiguity_signal import (
    filter_tools_to_clarify_only,
    is_financial_ambiguous,
)


def test_misbehaving_adapter_cannot_call_data_tool():
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


def test_force_tool_choice_passed_to_adapter():
    """Verify orchestrator passes tool_choice={"type":"function","function":{"name":"clarify"}} to OpenAI."""
    from app.services.chat.adapters.openai_adapter import OpenAIAdapter

    adapter = OpenAIAdapter(api_key="test")
    forcing = adapter.force_tool_choice("clarify")
    assert forcing == {"type": "function", "function": {"name": "clarify"}}


def test_force_tool_choice_model_param_ignored_for_openai():
    """OpenAI shape is model-agnostic — `model` is accepted only for protocol uniformity."""
    from app.services.chat.adapters.openai_adapter import OpenAIAdapter

    adapter = OpenAIAdapter(api_key="test")
    with_model = adapter.force_tool_choice("clarify", model="gpt-4o")
    without_model = adapter.force_tool_choice("clarify")
    assert (
        with_model
        == without_model
        == {
            "type": "function",
            "function": {"name": "clarify"},
        }
    )


def test_force_tool_choice_rejects_empty_tool_name():
    """Defensive: empty/non-string tool_name must raise ValueError, not produce a malformed param."""
    from app.services.chat.adapters.openai_adapter import OpenAIAdapter

    adapter = OpenAIAdapter(api_key="test")
    with pytest.raises(ValueError):
        adapter.force_tool_choice("")
