"""Contract test — Gemini adapter cannot bypass Plan Mode gate.

Mock adapter intentionally tries to call BigQuery first when financial-ambiguous
regex matches. Server-side filter must prevent it: BigQuery tool isn't in the
schema the adapter sees.
"""

import pytest

from app.services.chat.plan_mode.ambiguity_signal import (
    filter_tools_to_clarify_only,
    is_financial_ambiguous,
)
from app.services.chat.plan_mode.errors import PlanModeUnsupportedError


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


# `force_tool_choice` returns the internal `{type: tool, name: ...}` shape for
# all providers. Each adapter's `_convert_tool_choice` / `create_message`
# translates internal → native at the API call site. For Gemini, the native
# shape is `function_calling_config.mode='ANY'` + `allowed_function_names=[...]`,
# built inside `create_message`. End-to-end plumbing is verified by
# `test_*_reaches_api_kwargs` in `tests/test_llm_adapters.py`. The model-version
# gating that raises `PlanModeUnsupportedError` for sub-1.5 / missing model
# stays here — see `test_force_tool_choice_rejects_legacy_models` and
# `test_force_tool_choice_requires_model_for_gemini` below.
def test_force_tool_choice_passed_to_adapter():
    """Verify the Gemini adapter returns the internal tool_choice shape for a 1.5+ model."""
    from app.services.chat.adapters.gemini_adapter import GeminiAdapter

    adapter = GeminiAdapter(api_key="test")
    forcing = adapter.force_tool_choice("clarify", model="gemini-1.5-pro")
    assert forcing == {"type": "tool", "name": "clarify"}


def test_force_tool_choice_supports_gemini_2x():
    """Gemini 2.x family also supports function_calling_config.mode='ANY'."""
    from app.services.chat.adapters.gemini_adapter import GeminiAdapter

    adapter = GeminiAdapter(api_key="test")
    forcing = adapter.force_tool_choice("clarify", model="gemini-2.0-flash")
    assert forcing == {"type": "tool", "name": "clarify"}


def test_force_tool_choice_rejects_legacy_models():
    """Sub-1.5 Gemini models must raise PlanModeUnsupportedError so orchestrator can degrade gracefully."""
    from app.services.chat.adapters.gemini_adapter import GeminiAdapter

    adapter = GeminiAdapter(api_key="test")
    with pytest.raises(PlanModeUnsupportedError):
        adapter.force_tool_choice("clarify", model="gemini-1.0-pro")


def test_force_tool_choice_requires_model_for_gemini():
    """Gemini needs the model name to gate by version; missing model must raise PlanModeUnsupportedError."""
    from app.services.chat.adapters.gemini_adapter import GeminiAdapter

    adapter = GeminiAdapter(api_key="test")
    with pytest.raises(PlanModeUnsupportedError):
        adapter.force_tool_choice("clarify")


def test_force_tool_choice_rejects_empty_tool_name():
    """Defensive: empty/non-string tool_name must raise ValueError, not produce a malformed param."""
    from app.services.chat.adapters.gemini_adapter import GeminiAdapter

    adapter = GeminiAdapter(api_key="test")
    with pytest.raises(ValueError):
        adapter.force_tool_choice("", model="gemini-1.5-pro")
