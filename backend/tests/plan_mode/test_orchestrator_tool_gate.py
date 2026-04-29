"""Verify Plan Mode tool gate helpers + orchestrator wiring (unit tests)."""

import inspect
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


def test_orchestrator_sets_plan_mode_augmentation_on_unified_agent():
    """Codex P1: the orchestrator MUST set the augmentation on the agent
    instance via attribute, not append to a local `system_prompt` variable
    that the UnifiedAgent never reads.

    Source-level invariant: after `maybe_augment_for_plan_mode(...)`, the
    orchestrator must assign the result to `unified_agent._plan_mode_augmentation`
    (NOT just `system_prompt += ...`), because UnifiedAgent.system_prompt is
    a property that builds its own prompt and ignores the local variable.
    """
    from app.services.chat import orchestrator

    source = inspect.getsource(orchestrator)

    # Locate the augmentation assignment (now multi-line: a ternary that
    # gates the call on `plan_mode_resume_source is None`).
    aug_idx = source.index("plan_mode_augmentation = ")
    # Look in a window of ~2000 chars after the assignment for the agent
    # attribute write — the assignment + agent constructor are multi-line
    # so we need more breathing room than the local-mutation version did.
    window = source[aug_idx : aug_idx + 2000]

    assert "maybe_augment_for_plan_mode" in window, (
        "Augmentation block must still call maybe_augment_for_plan_mode."
    )
    assert "unified_agent._plan_mode_augmentation" in window, (
        "Orchestrator must set unified_agent._plan_mode_augmentation so the "
        "augmentation reaches the agent's system_prompt property — appending "
        "to the local `system_prompt` variable is dead code on the UnifiedAgent path."
    )


def test_orchestrator_sets_plan_mode_resume_directive_on_unified_agent():
    """Same fix as augmentation — the resume directive must reach the agent
    via attribute, not the dead local `system_prompt` variable.
    """
    from app.services.chat import orchestrator

    source = inspect.getsource(orchestrator)

    # Locate the resume directive append line.
    directive_idx = source.index("if plan_mode_resume_directive:")
    window = source[directive_idx : directive_idx + 600]

    assert "unified_agent._plan_mode_resume_directive" in window, (
        "Orchestrator must set unified_agent._plan_mode_resume_directive so the "
        "directive reaches the agent's system_prompt property — appending "
        "to the local `system_prompt` variable is dead code on the UnifiedAgent path."
    )


def test_resume_turn_skips_clarify_augmentation():
    """Codex P2 Bug 1: on resume turns, the clarify augmentation MUST NOT be
    injected.

    On a `plan_mode_choice` resume, `sanitized_input` is still the original
    financial query (e.g. "what's our revenue this quarter?"). The resume
    tool filter strips `clarify` from the inventory (only chosen-source
    tools + cross-source remain). If we ALSO inject the
    "CLARIFICATION REQUIRED" augmentation block, the model gets contradictory
    instructions: "your only first action is `clarify`" but `clarify` isn't
    in the inventory.

    Source-level invariant: the `maybe_augment_for_plan_mode(...)` call in
    `run_chat_turn` must be gated by `plan_mode_resume_source is None` so
    that resume turns receive an empty/None augmentation.
    """
    from app.services.chat import orchestrator

    source = inspect.getsource(orchestrator)

    aug_idx = source.index("plan_mode_augmentation = ")
    # Look at a window large enough to cover the call expression itself.
    window = source[aug_idx : aug_idx + 600]

    assert "maybe_augment_for_plan_mode" in window, (
        "Augmentation block must call maybe_augment_for_plan_mode."
    )
    assert "plan_mode_resume_source is None" in window, (
        "On resume turns (`plan_mode_resume_source` is set), the orchestrator "
        "must skip the clarify augmentation. Otherwise the prompt instructs "
        "the model to call `clarify` while the resume tool filter has stripped "
        "it from the inventory — contradictory instructions."
    )
