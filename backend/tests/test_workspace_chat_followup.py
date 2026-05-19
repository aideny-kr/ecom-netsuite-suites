"""Regression: workspace chat follow-up question behaves like memory loss.

Real session 1613e2ce-4d3c-471f-8cc6-e373a5fec6f9 on staging 2026-05-19:

  Turn 1: "is there any scripts that touches this item? FRAKMW000B"
          → agent answers correctly, citing FRANDVCH01 ID 5811, FRANCQ000Z ID 2201
  Turn 2: "what does it converts to?"
          → agent runs 5 workspace searches, all return 0 or wrong rows,
            final_text is empty, orchestrator substitutes
            _NO_RESULT_FALLBACK ("I wasn't able to find relevant information…")
          → user perceives this as "the chatbot forgot everything"

Two issues this test pins:

A) **Workspace prompt nudge.** The workspace context block should tell the
   agent to prefer recall from prior conversation on follow-up questions
   instead of re-running searches.

B) **Fallback wording.** When final_text is empty AND tools were called,
   the message must NOT say "I wasn't able to find relevant information" —
   that phrasing reads as "I have no knowledge of this" even though the
   prior turn already answered the question. It should instead acknowledge
   that the agent ran tools and ask the user to retry / refer back.
"""

from __future__ import annotations

# ─── Fix B: fallback wording ──────────────────────────────────────────────


def test_coerce_keeps_text_when_present():
    """Non-empty final_text always wins, regardless of tool_calls."""
    from app.services.chat.orchestrator import _coerce_assistant_content

    assert _coerce_assistant_content("Hello.", None) == "Hello."
    assert _coerce_assistant_content("Hello.", None, tool_calls=[{"tool": "x"}]) == "Hello."


def test_coerce_uses_no_result_fallback_when_no_tools_were_called():
    """Empty text + zero tool calls = same boilerplate as before.

    This is the "agent had nothing to say and didn't even try" case — the
    original fallback wording is appropriate.
    """
    from app.services.chat.orchestrator import _NO_RESULT_FALLBACK, _coerce_assistant_content

    assert _coerce_assistant_content("", None) == _NO_RESULT_FALLBACK
    assert _coerce_assistant_content(None, None) == _NO_RESULT_FALLBACK
    assert _coerce_assistant_content("", None, tool_calls=[]) == _NO_RESULT_FALLBACK
    assert _coerce_assistant_content("", None, tool_calls=None) == _NO_RESULT_FALLBACK


def test_coerce_uses_tool_spiral_fallback_when_tools_ran_with_empty_text():
    """Empty text + at least one tool call = agent went into a tool spiral.

    The fallback must NOT phrase this as "I couldn't find anything" because
    that reads like memory loss. It should acknowledge that tools ran and
    nudge the user to retry.
    """
    from app.services.chat.orchestrator import _NO_RESULT_FALLBACK, _coerce_assistant_content

    out = _coerce_assistant_content(
        "",
        None,
        tool_calls=[{"tool": "workspace_search", "result_summary": "No rows returned"}],
    )
    assert out
    assert out != _NO_RESULT_FALLBACK
    # The new wording must avoid the misleading "find relevant information"
    # phrase — that's what made users think the chatbot forgot prior context.
    low = out.lower()
    assert "find relevant information" not in low
    # And it should reference the tool activity so the user understands
    # this wasn't a knowledge gap.
    assert "tool" in low or "search" in low or "retry" in low or "again" in low


def test_coerce_clarification_still_returns_empty_with_tool_calls():
    """Backward compat: clarification card path is unchanged when tools ran."""
    from app.services.chat.orchestrator import _coerce_assistant_content

    persisted = {"type": "clarification", "status": "pending", "options": []}
    assert _coerce_assistant_content("", persisted, tool_calls=[{"tool": "x"}]) == ""
    assert _coerce_assistant_content("leaked text", persisted, tool_calls=[{"tool": "x"}]) == ""


# ─── Fix A: workspace prompt nudge ────────────────────────────────────────


def test_workspace_context_block_instructs_prior_context_preference():
    """The workspace context appended to the system prompt must tell the
    agent to prefer recalling from prior conversation on follow-up questions
    rather than re-running searches.

    Without this nudge, the agent's bias is "use workspace tools to browse"
    which causes it to re-search for terms it already answered with — and
    when those searches come back empty, it produces no final text and the
    user sees the boilerplate fallback (looks like memory loss).
    """
    from app.services.chat.orchestrator import _build_workspace_context_block

    block = _build_workspace_context_block(
        workspace_name="NetSuite Scripts",
        workspace_id="f504704c-1601-43c0-ae8c-0f77e9bef6c0",
        file_paths=["SuiteScripts/user_event/Framework_SalesOrder_UE.js"],
    )

    # Workspace identity is still present
    assert "NetSuite Scripts" in block
    assert "f504704c-1601-43c0-ae8c-0f77e9bef6c0" in block
    assert "SuiteScripts/user_event/Framework_SalesOrder_UE.js" in block

    # Prior-context preference instruction is present. The wording can vary
    # but must mention conversation history / prior answer / follow-up and
    # nudge against unnecessary re-tooling.
    low = block.lower()
    assert (
        "conversation history" in low
        or "prior answer" in low
        or "prior turn" in low
        or "earlier turn" in low
        or "previous answer" in low
    )
    assert "follow-up" in low or "followup" in low


def test_workspace_context_block_truncates_long_file_lists():
    """Backward compat: file listing is capped at 50 entries with a tail
    summary — same behavior as the inline implementation."""
    from app.services.chat.orchestrator import _build_workspace_context_block

    paths = [f"SuiteScripts/file_{i}.js" for i in range(75)]
    block = _build_workspace_context_block(
        workspace_name="WS",
        workspace_id="00000000-0000-0000-0000-000000000000",
        file_paths=paths,
    )
    assert "file_0.js" in block
    assert "file_49.js" in block
    assert "file_50.js" not in block  # past the cap
    assert "25 more" in block  # tail summary
