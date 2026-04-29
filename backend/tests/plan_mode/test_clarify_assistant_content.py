"""Codex P2 regression: assistant content must be empty for clarification turns.

The bug: ``orchestrator.run_chat_turn`` persists the assistant message with
``content=final_text or "I wasn't able to find relevant information for that question."``.
On a clarification turn the agent yields a tool_use (no text) → ``final_text``
is empty → the fallback string is persisted → the user sees
"I wasn't able to find relevant information..." rendered ABOVE the
clarification card. CFO confusion guaranteed.

Fix: when ``_persisted_output`` (or ``last_structured_output`` in the legacy
pipeline) carries ``type == "clarification"``, the message body should be
empty — the card IS the message.
"""

from __future__ import annotations

import pytest


def test_helper_returns_final_text_when_present():
    from app.services.chat.orchestrator import _coerce_assistant_content

    assert _coerce_assistant_content("Hello world.", None) == "Hello world."


def test_helper_returns_fallback_when_empty_and_no_structured_output():
    from app.services.chat.orchestrator import _coerce_assistant_content

    out = _coerce_assistant_content("", None)
    # Some non-empty fallback is fine — what matters is it isn't the
    # confusing "I wasn't able to find..." string when a card is present.
    assert out  # truthy
    assert isinstance(out, str)


def test_helper_returns_empty_for_clarification_structured_output():
    """The card IS the message; assistant content must be empty."""
    from app.services.chat.orchestrator import _coerce_assistant_content

    persisted = {
        "type": "clarification",
        "status": "pending",
        "options": [],
        "ambiguity_summary": "Revenue can mean two things.",
    }
    assert _coerce_assistant_content("", persisted) == ""


def test_helper_returns_empty_for_clarification_even_with_text():
    """If the agent leaks text alongside a clarification card, drop it.

    The agent prompt instructs no text on clarify turns, but if it slips
    through we still want the card to stand alone — the leaked text would
    sit ABOVE the card and confuse the user.
    """
    from app.services.chat.orchestrator import _coerce_assistant_content

    persisted = {"type": "clarification", "status": "pending", "options": []}
    # Even with non-empty final_text, clarification wins → empty content.
    assert _coerce_assistant_content("Some leaked preamble.", persisted) == ""


def test_helper_does_not_suppress_for_other_structured_output_types():
    """Only ``type == "clarification"`` triggers suppression — other types
    (data_table, write_confirmation, financial_report, etc.) do NOT.
    """
    from app.services.chat.orchestrator import _coerce_assistant_content

    persisted = {"type": "data_table", "data": {"columns": [], "rows": []}}
    # data_table renders below assistant content, so the fallback is fine here.
    out = _coerce_assistant_content("", persisted)
    assert out  # non-empty fallback is appropriate


def test_helper_does_not_suppress_for_charts_only_payload():
    """``_persisted_output`` may be a charts-only dict (no ``type`` key) —
    still should NOT suppress assistant content.
    """
    from app.services.chat.orchestrator import _coerce_assistant_content

    persisted = {"charts": [{"kind": "bar", "data": []}]}
    out = _coerce_assistant_content("", persisted)
    assert out


def test_helper_handles_none_persisted_output_gracefully():
    from app.services.chat.orchestrator import _coerce_assistant_content

    assert _coerce_assistant_content("Real text.", None) == "Real text."


@pytest.mark.parametrize("falsy", [None, ""])
def test_helper_returns_fallback_for_falsy_text_no_structured_output(falsy):
    from app.services.chat.orchestrator import _coerce_assistant_content

    out = _coerce_assistant_content(falsy or "", None)
    assert out  # non-empty fallback


def test_helper_is_used_in_run_chat_turn_persistence_site():
    """Static check: the run_chat_turn ChatMessage save site uses the helper."""
    import inspect

    from app.services.chat import orchestrator

    src = inspect.getsource(orchestrator.run_chat_turn)
    # Either the helper is called directly, or the literal coercion pattern
    # ``_coerce_assistant_content(final_text, _persisted_output)`` appears.
    assert "_coerce_assistant_content" in src, (
        "run_chat_turn must use _coerce_assistant_content for the assistant "
        "content; otherwise the clarification card has the fallback string above it."
    )


def test_helper_is_used_in_run_chat_pipeline_persistence_site():
    """Static check: the legacy single-agent loop save site uses the helper."""
    import inspect

    from app.services.chat import orchestrator

    src = inspect.getsource(orchestrator)
    # Find _run_chat_pipeline source (or whichever function holds the second
    # ChatMessage save). Either way, the literal fallback string should
    # appear at MOST inside _coerce_assistant_content's body — every other
    # ChatMessage(content=...) site should route through the helper.
    fallback = "I wasn't able to find relevant information for that question"
    occurrences = src.count(fallback)
    # One in helper definition is OK; more than that means a save site is
    # bypassing the helper.
    assert occurrences <= 1, (
        f"Found {occurrences} occurrences of the fallback string. Only the "
        "helper's body should contain it; ChatMessage save sites should call "
        "_coerce_assistant_content."
    )
