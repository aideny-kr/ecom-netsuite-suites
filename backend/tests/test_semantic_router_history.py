"""The Tier 2 router must inherit topic context from prior turns.

Real incident: user asked 'analyze Heap pageview funnel' (router correctly
picked bi-agent). The next turn was 'go ahead with step 1'. Without history,
Haiku classified the follow-up as generic and routed to unified-agent,
which denied BigQuery capability. With the last N messages attached, Haiku
can see the conversation is about BigQuery and keep bi-agent selected."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.chat.llm_adapter import LLMResponse
from app.services.chat.routing.semantic_router import SemanticRouter


def _agent(agent_id: str, description: str):
    return SimpleNamespace(agent_id=agent_id, description=description)


@pytest.mark.asyncio
async def test_route_accepts_history_kwarg():
    """Signature check: route() must take an optional `history` kwarg."""
    router = SemanticRouter()
    adapter = SimpleNamespace(create_message=AsyncMock(return_value=LLMResponse(text_blocks=["unified-agent"])))
    agents = [_agent("bi-agent", "BigQuery analytics")]
    result = await router.route(
        query="go ahead",
        available_agents=agents,
        adapter=adapter,
        history=[{"role": "user", "content": "analyze Heap pageview funnel"}],
    )
    assert result in {"bi-agent", "unified-agent"}


@pytest.mark.asyncio
async def test_history_included_in_haiku_prompt():
    """The last N messages must show up in the classifier prompt."""
    router = SemanticRouter()
    adapter = SimpleNamespace(create_message=AsyncMock(return_value=LLMResponse(text_blocks=["bi-agent"])))
    agents = [_agent("bi-agent", "BigQuery analytics")]
    await router.route(
        query="go ahead with step 1",
        available_agents=agents,
        adapter=adapter,
        history=[
            {"role": "user", "content": "analyze Heap pageview funnel"},
            {"role": "assistant", "content": "Here's the plan for BigQuery..."},
        ],
    )
    call_kwargs = adapter.create_message.await_args.kwargs
    system = call_kwargs.get("system", "")
    assert "Heap pageview" in system, "history must be surfaced to Haiku"


@pytest.mark.asyncio
async def test_route_without_history_still_works():
    """Backwards compatibility: existing callers pass no history."""
    router = SemanticRouter()
    adapter = SimpleNamespace(create_message=AsyncMock(return_value=LLMResponse(text_blocks=["unified-agent"])))
    agents = [_agent("bi-agent", "BigQuery analytics")]
    result = await router.route(
        query="hello",
        available_agents=agents,
        adapter=adapter,
    )
    assert result == "unified-agent"
