"""Anthropic adapter: force_tool_choice support for Plan Mode."""

import pytest

from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter
from app.services.chat.plan_mode.adapter_protocol import ForceToolChoiceCapable


def test_force_tool_choice_returns_anthropic_shape():
    """Returns the dict Anthropic SDK expects for tool_choice forcing."""
    adapter = AnthropicAdapter(api_key="test-key")
    result = adapter.force_tool_choice("clarify")
    assert result == {"type": "tool", "name": "clarify"}


def test_force_tool_choice_ignores_model_arg():
    """Anthropic shape is model-agnostic; passing model has no effect."""
    adapter = AnthropicAdapter(api_key="test-key")
    result = adapter.force_tool_choice("clarify", model="claude-sonnet-4-6")
    assert result == {"type": "tool", "name": "clarify"}


def test_force_tool_choice_validates_tool_name():
    """Empty/None tool_name raises ValueError (not PlanModeUnsupportedError)."""
    adapter = AnthropicAdapter(api_key="test-key")
    with pytest.raises(ValueError, match="tool_name"):
        adapter.force_tool_choice("")


def test_anthropic_adapter_implements_protocol():
    """Runtime check: AnthropicAdapter satisfies ForceToolChoiceCapable."""
    adapter = AnthropicAdapter(api_key="test-key")
    assert isinstance(adapter, ForceToolChoiceCapable)
