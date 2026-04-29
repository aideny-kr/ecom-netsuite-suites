"""Integration-style tests for clarify intercept in BaseSpecialistAgent.run_streaming.

Exercises the new clarify intercept path inserted before the mutation intercept:
- On valid clarify input → yields ``clarification_required`` event + terminal
  ``response`` event with empty data.
- On invalid clarify input → no ``clarification_required``; the agent retries
  within the turn (the second LLM response is consumed normally).

A minimal TestAgent subclass avoids the heavy UnifiedAgent._setup_context path.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.chat.agents.base_agent import BaseSpecialistAgent
from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock


class _TestAgent(BaseSpecialistAgent):
    """Minimal concrete agent for exercising run_streaming end-to-end."""

    def __init__(self, tool_defs: list[dict], connectors: list[Any]) -> None:
        super().__init__(uuid.uuid4(), uuid.uuid4(), "test-correlation")
        self._tool_defs = tool_defs
        self._connectors = connectors

    @property
    def agent_name(self) -> str:
        return "test"

    @property
    def system_prompt(self) -> str:
        return "test prompt"

    @property
    def tool_definitions(self) -> list[dict]:
        return self._tool_defs


def _make_clarify_response(tool_input: dict, response_id: str = "msg_1") -> LLMResponse:
    """Build a fake LLMResponse with a clarify tool_use block."""
    return LLMResponse(
        text_blocks=[],
        tool_use_blocks=[ToolUseBlock(id=response_id, name="clarify", input=tool_input)],
        usage=TokenUsage(
            input_tokens=10,
            output_tokens=20,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


def _valid_clarify_input() -> dict:
    return {
        "options": [
            {
                "id": "A",
                "title": "NetSuite GL",
                "rationale": "GL recognized revenue",
                "source": "netsuite",
                "is_default": True,
            },
            {
                "id": "B",
                "title": "BigQuery",
                "rationale": "checkout totals",
                "source": "bigquery",
                "is_default": False,
            },
        ],
        "ambiguity_summary": "Revenue can mean two things.",
    }


def _make_mock_adapter(*responses: LLMResponse):
    """Mock adapter whose stream_message yields ('response', resp) for each call.

    Successive calls draw the next response from the queue. If the agent loop
    exhausts the queue, the last response is reused (defensive).
    """
    adapter = MagicMock()
    queue = list(responses)
    state = {"i": 0}

    async def _stream(*args, **kwargs):
        idx = min(state["i"], len(queue) - 1)
        state["i"] += 1
        yield ("response", queue[idx])

    adapter.stream_message = _stream
    adapter.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": []})
    return adapter


@pytest.mark.asyncio
async def test_clarify_intercept_yields_clarification_required(monkeypatch):
    """Agent emitting clarify tool_use yields clarification_required + terminal response."""

    # Avoid hitting confidence/learned-rules helpers
    async def _noop_pattern(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "app.services.chat.agents.base_agent._maybe_store_query_pattern",
        _noop_pattern,
    )

    # Patch get_active_policy: BaseSpecialistAgent.run_streaming awaits it.
    async def _get_policy(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.policy_service.get_active_policy", _get_policy)

    adapter = _make_mock_adapter(_make_clarify_response(_valid_clarify_input()))

    connectors = [MagicMock(provider="netsuite"), MagicMock(provider="bigquery")]
    agent = _TestAgent(tool_defs=[{"name": "clarify"}], connectors=connectors)

    db = AsyncMock()

    events = []
    async for evt in agent.run_streaming(
        task="What's our revenue?",
        context={},
        db=db,
        adapter=adapter,
        model="claude-sonnet-4-6",
        session_id="sess-1",
    ):
        events.append(evt)

    event_types = [e[0] for e in events]
    assert "clarification_required" in event_types, f"expected clarification_required in {event_types}"
    assert "response" in event_types

    # Clarification payload has the structured_output shape
    clarif_event = next(e for e in events if e[0] == "clarification_required")
    payload = clarif_event[1]
    assert payload["type"] == "clarification"
    assert payload["status"] == "pending"
    assert "confirmation_token" in payload
    assert payload["default_id"] == "A"

    # Final response has empty data
    response_event = next(e for e in events if e[0] == "response")
    response = response_event[1]
    assert response.success is True
    assert response.data == ""


@pytest.mark.asyncio
async def test_clarify_intercept_error_feeds_back_to_agent(monkeypatch):
    """Invalid clarify input → agent gets is_error=True tool_result and retries."""
    bad_input = {
        "options": [
            {"id": "A", "title": "x", "rationale": "y", "source": "netsuite", "is_default": False},
            {"id": "B", "title": "x", "rationale": "y", "source": "bigquery", "is_default": False},
        ],
        "ambiguity_summary": "summary",
    }

    # After the bad clarify call, the agent retries — second response is plain text.
    text_response = LLMResponse(
        text_blocks=["OK, I'll just answer with NetSuite."],
        tool_use_blocks=[],
        usage=TokenUsage(
            input_tokens=5,
            output_tokens=10,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )

    async def _noop_pattern(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "app.services.chat.agents.base_agent._maybe_store_query_pattern",
        _noop_pattern,
    )

    async def _get_policy(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.policy_service.get_active_policy", _get_policy)

    # Stub out structured-confidence extraction so the test doesn't hit Haiku
    from app.services.confidence_extractor import ConfidenceAssessment

    async def _stub_assess(**kwargs):
        return ConfidenceAssessment(score=4, reasoning="ok", source="default")

    monkeypatch.setattr(
        "app.services.chat.agents.base_agent.extract_structured_confidence",
        _stub_assess,
    )

    adapter = _make_mock_adapter(_make_clarify_response(bad_input), text_response)

    connectors = [MagicMock(provider="netsuite"), MagicMock(provider="bigquery")]
    agent = _TestAgent(tool_defs=[{"name": "clarify"}], connectors=connectors)

    db = AsyncMock()

    events = []
    async for evt in agent.run_streaming(
        task="What's our revenue?",
        context={},
        db=db,
        adapter=adapter,
        model="claude-sonnet-4-6",
        session_id="sess-1",
    ):
        events.append(evt)

    # On error, NO clarification_required event was yielded
    event_types = [e[0] for e in events]
    assert "clarification_required" not in event_types, f"unexpected clarification in {event_types}"

    # The agent's tool_end for clarify should be marked unsuccessful
    tool_end_events = [e for e in events if e[0] == "tool_end"]
    assert any(te[1].get("tool_name") == "clarify" and te[1].get("success") is False for te in tool_end_events), (
        f"expected an unsuccessful clarify tool_end in {tool_end_events}"
    )

    # The final response is the text from the second LLM call (after the error
    # tool_result was fed back)
    response_event = next(e for e in events if e[0] == "response")
    assert response_event[1].data == "OK, I'll just answer with NetSuite."
