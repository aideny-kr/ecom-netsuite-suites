"""Verify Plan Mode tool gate helpers + orchestrator wiring (unit tests)."""

from unittest.mock import MagicMock

import pytest

from app.services.chat.plan_mode.ambiguity_signal import (
    filter_tools_to_clarify_only,
    try_force_tool_choice,
)
from app.services.chat.plan_mode.errors import PlanModeUnsupportedError


def test_filter_tools_to_clarify_only_keeps_only_clarify():
    full_inventory = [
        {"name": "bigquery_sql", "description": "..."},
        {"name": "netsuite_suiteql", "description": "..."},
        {"name": "clarify", "description": "..."},
        {"name": "pivot_query_result", "description": "..."},
    ]
    filtered = filter_tools_to_clarify_only(full_inventory)
    names = [t["name"] for t in filtered]
    assert names == ["clarify"]


def test_filter_tools_to_clarify_only_empty_when_clarify_absent():
    """Defensive: if clarify isn't in inventory yet, returns empty list.

    Caller must check this and skip gate activation to avoid sending an
    empty tool list to the LLM.
    """
    inventory = [
        {"name": "bigquery_sql"},
        {"name": "netsuite_suiteql"},
    ]
    filtered = filter_tools_to_clarify_only(inventory)
    assert filtered == []


def test_filter_tools_to_clarify_only_empty_input():
    assert filter_tools_to_clarify_only([]) == []


def test_try_force_tool_choice_returns_dict_when_supported():
    adapter = MagicMock()
    adapter.force_tool_choice = MagicMock(return_value={"type": "tool", "name": "clarify"})
    result = try_force_tool_choice(adapter, "clarify")
    assert result == {"type": "tool", "name": "clarify"}
    adapter.force_tool_choice.assert_called_once_with("clarify")


def test_try_force_tool_choice_returns_dict_with_model():
    adapter = MagicMock()
    adapter.force_tool_choice = MagicMock(return_value={"type": "tool", "name": "clarify"})
    result = try_force_tool_choice(adapter, "clarify", model="gemini-1.5-pro")
    assert result == {"type": "tool", "name": "clarify"}
    adapter.force_tool_choice.assert_called_once_with("clarify", model="gemini-1.5-pro")


def test_try_force_tool_choice_returns_none_on_unsupported():
    """Adapter raising PlanModeUnsupportedError → graceful degradation (None)."""
    adapter = MagicMock()
    adapter.force_tool_choice = MagicMock(side_effect=PlanModeUnsupportedError("gemini-pro", reason="needs 1.5+"))
    result = try_force_tool_choice(adapter, "clarify", model="gemini-pro")
    assert result is None


def test_try_force_tool_choice_lets_other_errors_bubble():
    """Only PlanModeUnsupportedError is swallowed — real bugs surface."""
    adapter = MagicMock()
    adapter.force_tool_choice = MagicMock(side_effect=ValueError("boom"))
    with pytest.raises(ValueError, match="boom"):
        try_force_tool_choice(adapter, "clarify")
