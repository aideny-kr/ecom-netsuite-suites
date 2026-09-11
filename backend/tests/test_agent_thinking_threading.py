"""Task A4 — `thinking_level` threading through the agent loop.

Invariant under test: the `thinking_level` argument handed to the agent reaches
`adapter.stream_message` (the carrier). The escalation BUMP itself is wired in
Task A5; here we only prove the carrier threads the initial level.

Per the plan's implementer note, the scaffolding mocks the minimal collaborators
(`get_active_policy`, `extract_structured_confidence`) and runs a real
``UnifiedAgent`` with ``db=None`` so ``_setup_context`` falls through its
try/excepts to the local-tool fallback — the production threading path is
exercised end to end without a DB.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services.chat.llm_adapter import LLMResponse, ToolUseBlock
from app.services.confidence_extractor import ConfidenceAssessment


class _RecordingAdapter:
    """Minimal adapter that records thinking_level on each stream call."""

    def __init__(self):
        self.levels: list[str | None] = []

    def force_tool_choice(self, name, model=None):
        return {"type": "tool", "name": name}

    async def create_message(self, **kwargs):
        # The real UnifiedAgent now classifies even with one data source when
        # accounting tools are present; this adapter also models that call.
        assert kwargs["tools"][0]["name"] == "route_request"
        return LLMResponse(
            tool_use_blocks=[
                ToolUseBlock(id="route", name="route_request", input={"kind": "conversation", "continuation": False})
            ]
        )

    async def stream_message(self, **kwargs):
        self.levels.append(kwargs.get("thinking_level"))
        resp = LLMResponse(text_blocks=["done"])
        yield "text", "done"
        yield "response", resp

    def build_assistant_message(self, response):
        return {"role": "assistant", "content": [{"type": "text", "text": "done"}]}

    def build_tool_result_message(self, tool_results):
        return {"role": "user", "content": tool_results}


@pytest.mark.asyncio
async def test_run_streaming_passes_thinking_level_to_adapter():
    from app.services.chat.agents.unified_agent import UnifiedAgent

    agent = UnifiedAgent(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        correlation_id=str(uuid.uuid4()),
    )
    adapter = _RecordingAdapter()

    # Select the source explicitly so the separate source-selection gate does
    # not short-circuit this test of the adapter's thinking-level carrier.
    with (
        patch(
            "app.services.policy_service.get_active_policy",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.services.chat.agents.base_agent.extract_structured_confidence",
            new_callable=AsyncMock,
            return_value=ConfidenceAssessment(score=5, reasoning="", source="regex_fallback"),
        ),
    ):
        gen = agent.run_streaming(
            task="Explain NetSuite invoice terms.",
            context={},
            db=None,
            adapter=adapter,
            model="claude-sonnet-4-6",
            thinking_level="high",
        )
        async for _ in gen:
            pass

    assert adapter.levels and adapter.levels[0] == "high"
