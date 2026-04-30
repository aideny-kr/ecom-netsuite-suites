"""Verify Plan Mode adapter protocol + error type."""

import inspect

from app.services.chat.plan_mode.adapter_protocol import ForceToolChoiceCapable
from app.services.chat.plan_mode.errors import PlanModeUnsupportedError


def test_plan_mode_unsupported_error_carries_provider():
    err = PlanModeUnsupportedError("anthropic-old", reason="tool_choice not in API")
    assert err.provider == "anthropic-old"
    assert err.reason == "tool_choice not in API"
    assert "anthropic-old" in str(err)
    assert "tool_choice not in API" in str(err)


def test_plan_mode_unsupported_error_default_reason():
    err = PlanModeUnsupportedError("some-provider")
    assert err.provider == "some-provider"
    assert err.reason  # has SOME default reason


def test_force_tool_choice_protocol_has_method():
    """ForceToolChoiceCapable is a Protocol with force_tool_choice method."""
    methods = {name for name, _ in inspect.getmembers(ForceToolChoiceCapable)}
    assert "force_tool_choice" in methods
