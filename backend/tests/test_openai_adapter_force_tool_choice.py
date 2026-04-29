"""OpenAI adapter: force_tool_choice support for Plan Mode."""

import pytest

from app.services.chat.adapters.openai_adapter import OpenAIAdapter
from app.services.chat.plan_mode.adapter_protocol import ForceToolChoiceCapable


def test_force_tool_choice_returns_openai_shape():
    """OpenAI uses tool_choice={"type":"function","function":{"name":"..."}}."""
    adapter = OpenAIAdapter(api_key="test-key")
    result = adapter.force_tool_choice("clarify")
    assert result == {"type": "function", "function": {"name": "clarify"}}


def test_force_tool_choice_ignores_model_arg():
    """OpenAI shape is model-agnostic; passing model has no effect."""
    adapter = OpenAIAdapter(api_key="test-key")
    result = adapter.force_tool_choice("clarify", model="gpt-4o")
    assert result == {"type": "function", "function": {"name": "clarify"}}


def test_force_tool_choice_validates_tool_name():
    adapter = OpenAIAdapter(api_key="test-key")
    with pytest.raises(ValueError, match="tool_name"):
        adapter.force_tool_choice("")


def test_openai_adapter_implements_protocol():
    """Runtime check: OpenAIAdapter satisfies ForceToolChoiceCapable."""
    adapter = OpenAIAdapter(api_key="test-key")
    assert isinstance(adapter, ForceToolChoiceCapable)
