"""Gemini adapter: force_tool_choice support for Plan Mode.

Gemini uses function_calling_config.mode='ANY' + allowed_function_names=[...]
to force a specific tool. Available on Gemini 1.5+ and 2.x models.
"""

import pytest

from app.services.chat.adapters.gemini_adapter import GeminiAdapter
from app.services.chat.plan_mode.adapter_protocol import ForceToolChoiceCapable
from app.services.chat.plan_mode.errors import PlanModeUnsupportedError


def test_force_tool_choice_returns_gemini_shape_15():
    adapter = GeminiAdapter(api_key="test-key")
    result = adapter.force_tool_choice("clarify", model="gemini-1.5-pro")
    assert result == {
        "function_calling_config": {
            "mode": "ANY",
            "allowed_function_names": ["clarify"],
        }
    }


def test_force_tool_choice_returns_gemini_shape_20():
    adapter = GeminiAdapter(api_key="test-key")
    result = adapter.force_tool_choice("clarify", model="gemini-2.0-flash")
    assert result == {
        "function_calling_config": {
            "mode": "ANY",
            "allowed_function_names": ["clarify"],
        }
    }


def test_force_tool_choice_unsupported_on_old_models():
    """Gemini 1.0 / Pro 1.0 models don't support function_calling_config."""
    adapter = GeminiAdapter(api_key="test-key")
    with pytest.raises(PlanModeUnsupportedError) as exc_info:
        adapter.force_tool_choice("clarify", model="gemini-pro")
    assert "gemini-pro" in str(exc_info.value)


def test_force_tool_choice_requires_model_arg():
    """Gemini cannot decide compatibility without knowing the model."""
    adapter = GeminiAdapter(api_key="test-key")
    with pytest.raises(PlanModeUnsupportedError) as exc_info:
        adapter.force_tool_choice("clarify")
    assert "model" in str(exc_info.value).lower()


def test_force_tool_choice_validates_tool_name():
    adapter = GeminiAdapter(api_key="test-key")
    with pytest.raises(ValueError, match="tool_name"):
        adapter.force_tool_choice("", model="gemini-1.5-pro")


def test_gemini_adapter_implements_protocol():
    adapter = GeminiAdapter(api_key="test-key")
    assert isinstance(adapter, ForceToolChoiceCapable)
