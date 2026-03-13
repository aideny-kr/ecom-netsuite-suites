"""Tests for tool_choice threading through the agentic loop."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock


@pytest.mark.asyncio
async def test_run_passes_tool_choice_on_step_0_only():
    """tool_choice should be passed on step 0 and None on subsequent steps."""
    from app.services.chat.agents.base_agent import BaseSpecialistAgent

    class TestAgent(BaseSpecialistAgent):
        agent_name = "test"
        max_steps = 3
        @property
        def system_prompt(self):
            return "test prompt"
        @property
        def tool_definitions(self):
            return [{"name": "test_tool", "description": "test", "input_schema": {"type": "object", "properties": {}}}]

    agent = TestAgent.__new__(TestAgent)
    agent.tenant_id = None
    agent.user_id = None
    agent.correlation_id = "test"

    mock_adapter = MagicMock()

    tool_response = LLMResponse(
        text_blocks=[],
        tool_use_blocks=[ToolUseBlock(id="t1", name="test_tool", input={"query": "test"})],
        usage=TokenUsage(input_tokens=100, output_tokens=50),
    )
    text_response = LLMResponse(
        text_blocks=["Final answer"],
        tool_use_blocks=[],
        usage=TokenUsage(input_tokens=100, output_tokens=50),
    )
    mock_adapter.create_message = AsyncMock(side_effect=[tool_response, text_response])
    mock_adapter.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": []})
    mock_adapter.build_tool_result_message = MagicMock(return_value={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]})

    with patch("app.services.chat.tools.execute_tool_call", new_callable=AsyncMock) as mock_exec, \
         patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", new_callable=AsyncMock), \
         patch("app.services.chat.agents.base_agent.extract_structured_confidence", new_callable=AsyncMock) as mock_conf, \
         patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None):
        mock_exec.return_value = '{"success": true, "data": "ok"}'
        mock_conf.return_value = MagicMock(score=4, source="mock")

        await agent.run(
            task="test query",
            context={},
            db=AsyncMock(),
            adapter=mock_adapter,
            model="test-model",
            tool_choice={"type": "tool", "name": "test_tool"},
        )

    calls = mock_adapter.create_message.call_args_list
    assert calls[0][1]["tool_choice"] == {"type": "tool", "name": "test_tool"}
    assert calls[1][1].get("tool_choice") is None


@pytest.mark.asyncio
async def test_run_without_tool_choice_passes_none():
    """When tool_choice is not provided, all steps should have tool_choice=None."""
    from app.services.chat.agents.base_agent import BaseSpecialistAgent

    class TestAgent(BaseSpecialistAgent):
        agent_name = "test"
        max_steps = 1
        @property
        def system_prompt(self):
            return "test prompt"
        @property
        def tool_definitions(self):
            return []

    agent = TestAgent.__new__(TestAgent)
    agent.tenant_id = None
    agent.user_id = None
    agent.correlation_id = "test"

    mock_adapter = MagicMock()
    mock_adapter.create_message = AsyncMock(return_value=LLMResponse(
        text_blocks=["answer"],
        tool_use_blocks=[],
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    ))

    with patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", new_callable=AsyncMock), \
         patch("app.services.chat.agents.base_agent.extract_structured_confidence", new_callable=AsyncMock) as mock_conf, \
         patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None):
        mock_conf.return_value = MagicMock(score=4, source="mock")
        await agent.run(
            task="test",
            context={},
            db=AsyncMock(),
            adapter=mock_adapter,
            model="test-model",
        )

    call_kwargs = mock_adapter.create_message.call_args[1]
    assert call_kwargs.get("tool_choice") is None


@pytest.mark.asyncio
async def test_unified_agent_forwards_tool_choice():
    """UnifiedAgent.run_streaming() must accept and forward tool_choice."""
    import inspect
    from app.services.chat.agents.unified_agent import UnifiedAgent

    # Verify the signature accepts tool_choice
    sig = inspect.signature(UnifiedAgent.run_streaming)
    assert "tool_choice" in sig.parameters
    assert sig.parameters["tool_choice"].default is None

    sig_run = inspect.signature(UnifiedAgent.run)
    assert "tool_choice" in sig_run.parameters
    assert sig_run.parameters["tool_choice"].default is None
