"""Tests for the agentic chat orchestrator loop."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock
from app.services.chat.orchestrator import MAX_STEPS, run_chat_turn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm_response(
    text: str | None = None,
    tool_blocks: list[ToolUseBlock] | None = None,
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> LLMResponse:
    return LLMResponse(
        text_blocks=[text] if text else [],
        tool_use_blocks=tool_blocks or [],
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _make_session(tenant_id: uuid.UUID, messages=None):
    """Create a mock ChatSession."""
    session = MagicMock()
    session.id = uuid.uuid4()
    session.title = None
    session.messages = messages or []
    session.workspace_id = None
    session.session_type = "chat"
    return session


async def _collect_stream_result(async_gen):
    """Consume the run_chat_turn async generator and return the final message dict."""
    result = None
    async for chunk in async_gen:
        if chunk.get("type") == "message":
            result = chunk["message"]
    return result


def _make_stream_side_effect(responses):
    """Create a side_effect for stream_message that yields from LLMResponse objects.

    Wraps each LLMResponse as an async generator yielding ("text", text) then ("response", response).
    Supports a list of responses consumed in order (like AsyncMock side_effect).
    """
    call_count = 0

    async def stream_fn(**kwargs):
        nonlocal call_count
        resp = responses[call_count] if call_count < len(responses) else responses[-1]
        call_count += 1
        for text in resp.text_blocks:
            yield "text", text
        yield "response", resp

    return stream_fn


_DEFAULT_AI_CONFIG = ("anthropic", "claude-sonnet-4-20250514", "sk-test", False)
_SETTINGS = "app.services.chat.orchestrator.settings"
_ORCH = "app.services.chat.orchestrator"


def _patch_orchestrator(**overrides):
    """Context manager to patch orchestrator dependencies with adapter pattern."""
    defaults = {
        "get_tenant_ai_config": AsyncMock(return_value=_DEFAULT_AI_CONFIG),
        "retriever_node": AsyncMock(),
        "build_all_tool_definitions": AsyncMock(return_value=[]),
        "log_event": AsyncMock(),
        "get_active_template": AsyncMock(return_value="You are a helpful assistant."),
        "deduct_chat_credits": AsyncMock(return_value=None),
    }
    defaults.update(overrides)

    patches = {}
    for name, mock in defaults.items():
        patches[name] = patch(f"app.services.chat.orchestrator.{name}", new_callable=lambda m=mock: lambda: m)

    # Use contextlib-style manual patches
    import contextlib

    @contextlib.contextmanager
    def ctx():
        active = {}
        for name, patcher in patches.items():
            active[name] = patcher.start()
        try:
            yield active
        finally:
            for patcher in patches.values():
                patcher.stop()

    return ctx()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAgenticSingleStep:
    """Test that a simple text response exits the loop immediately."""

    @pytest.mark.asyncio
    async def test_text_only_response(self):
        """No tool calls — loop exits after 1 iteration."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        session = _make_session(tenant_id)

        text_response = _make_llm_response(text="Here are your orders.")

        mock_adapter = MagicMock()
        mock_adapter.create_message = AsyncMock(return_value=text_response)
        mock_adapter.stream_message = _make_stream_side_effect([text_response])

        db = AsyncMock(spec=AsyncSession)
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        with (
            patch.object(settings, "MULTI_AGENT_ENABLED", False),
            patch(f"{_ORCH}.get_tenant_ai_config", new_callable=AsyncMock, return_value=_DEFAULT_AI_CONFIG),
            patch(f"{_ORCH}.get_adapter", return_value=mock_adapter),
            patch(f"{_ORCH}.retriever_node", new_callable=AsyncMock),
            patch(f"{_ORCH}.build_all_tool_definitions", new_callable=AsyncMock, return_value=[]),
            patch(f"{_ORCH}.log_event", new_callable=AsyncMock),
            patch(f"{_ORCH}.get_active_template", new_callable=AsyncMock, return_value="You are a helpful assistant."),
            patch(f"{_ORCH}.deduct_chat_credits", new_callable=AsyncMock, return_value=None),
            patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
        ):
            result = await _collect_stream_result(
                run_chat_turn(
                    db=db,
                    session=session,
                    user_message="Show my orders",
                    user_id=user_id,
                    tenant_id=tenant_id,
                )
            )

        assert result["content"] == "Here are your orders."
        assert result["role"] == "assistant"


class TestAgenticToolCall:
    """Test tool call + answer flow."""

    @pytest.mark.asyncio
    async def test_tool_call_then_answer(self):
        """Tool executed, result fed back, adapter responds with text."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        session = _make_session(tenant_id)

        tool_only = _make_llm_response(
            tool_blocks=[
                ToolUseBlock(
                    id="tool_1",
                    name="data_sample_table_read",
                    input={"table_name": "orders"},
                )
            ],
        )
        text_response = _make_llm_response(text="You have 5 orders.")

        mock_adapter = MagicMock()
        mock_adapter.create_message = AsyncMock(side_effect=[tool_only, text_response])
        mock_adapter.stream_message = _make_stream_side_effect([tool_only, text_response])
        mock_adapter.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": []})
        mock_adapter.build_tool_result_message = MagicMock(return_value={"role": "user", "content": []})

        db = AsyncMock(spec=AsyncSession)
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        with (
            patch.object(settings, "MULTI_AGENT_ENABLED", False),
            patch(f"{_ORCH}.get_tenant_ai_config", new_callable=AsyncMock, return_value=_DEFAULT_AI_CONFIG),
            patch(f"{_ORCH}.get_adapter", return_value=mock_adapter),
            patch(f"{_ORCH}.retriever_node", new_callable=AsyncMock),
            patch(
                f"{_ORCH}.build_all_tool_definitions",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "name": "data_sample_table_read",
                        "description": "Read table",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ],
            ),
            patch(
                f"{_ORCH}.execute_tool_call",
                new_callable=AsyncMock,
                return_value='{"data": [{"id": 1}]}',
            ),
            patch(f"{_ORCH}.log_event", new_callable=AsyncMock),
            patch(f"{_ORCH}.get_active_template", new_callable=AsyncMock, return_value="You are a helpful assistant."),
            patch(f"{_ORCH}.deduct_chat_credits", new_callable=AsyncMock, return_value=None),
            patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
        ):
            result = await _collect_stream_result(
                run_chat_turn(
                    db=db,
                    session=session,
                    user_message="How many orders?",
                    user_id=user_id,
                    tenant_id=tenant_id,
                )
            )

        assert result["content"] == "You have 5 orders."


class TestAgenticToolRetry:
    """Test error recovery — adapter retries with corrected params."""

    @pytest.mark.asyncio
    async def test_tool_error_then_retry(self):
        """First tool call fails, adapter retries with corrected params."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        session = _make_session(tenant_id)

        response1 = _make_llm_response(
            tool_blocks=[
                ToolUseBlock(
                    id="tool_1",
                    name="netsuite_suiteql",
                    input={"query": "SELECT total FROM transaction"},
                )
            ],
        )
        response2 = _make_llm_response(
            tool_blocks=[
                ToolUseBlock(
                    id="tool_2",
                    name="netsuite_suiteql",
                    input={"query": "SELECT amount FROM transaction"},
                )
            ],
        )
        response3 = _make_llm_response(text="The total is $1000.")

        tool_results = [
            '{"error": "Unknown identifier: total"}',
            '{"rows": [{"amount": 1000}]}',
        ]

        mock_adapter = MagicMock()
        mock_adapter.create_message = AsyncMock(side_effect=[response1, response2, response3])
        mock_adapter.stream_message = _make_stream_side_effect([response1, response2, response3])
        mock_adapter.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": []})
        mock_adapter.build_tool_result_message = MagicMock(return_value={"role": "user", "content": []})

        db = AsyncMock(spec=AsyncSession)
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        with (
            patch.object(settings, "MULTI_AGENT_ENABLED", False),
            patch(f"{_ORCH}.get_tenant_ai_config", new_callable=AsyncMock, return_value=_DEFAULT_AI_CONFIG),
            patch(f"{_ORCH}.get_adapter", return_value=mock_adapter),
            patch(f"{_ORCH}.retriever_node", new_callable=AsyncMock),
            patch(
                f"{_ORCH}.build_all_tool_definitions",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "name": "netsuite_suiteql",
                        "description": "Query",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ],
            ),
            patch(
                f"{_ORCH}.execute_tool_call",
                new_callable=AsyncMock,
                side_effect=tool_results,
            ),
            patch(f"{_ORCH}.log_event", new_callable=AsyncMock),
            patch(f"{_ORCH}.get_active_template", new_callable=AsyncMock, return_value="You are a helpful assistant."),
            patch(f"{_ORCH}.deduct_chat_credits", new_callable=AsyncMock, return_value=None),
            patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
        ):
            result = await _collect_stream_result(
                run_chat_turn(
                    db=db,
                    session=session,
                    user_message="What is the total?",
                    user_id=user_id,
                    tenant_id=tenant_id,
                )
            )

        assert result["content"] == "The total is $1000."
        assert result["tool_calls"] is not None
        assert len(result["tool_calls"]) == 2


class TestAgenticMaxSteps:
    """Test max steps exhaustion."""

    @pytest.mark.asyncio
    async def test_loop_exhaustion_forces_text(self):
        """When loop exhausts MAX_STEPS, a final text-only call is made."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        session = _make_session(tenant_id)

        tool_responses = [
            _make_llm_response(
                tool_blocks=[
                    ToolUseBlock(
                        id=f"tool_{i}",
                        name="data_sample_table_read",
                        input={"table_name": "orders"},
                    )
                ],
            )
            for i in range(MAX_STEPS)
        ]
        final_response = _make_llm_response(text="I've exhausted my tool calls.")

        mock_adapter = MagicMock()
        mock_adapter.create_message = AsyncMock(side_effect=tool_responses + [final_response])
        mock_adapter.stream_message = _make_stream_side_effect(tool_responses + [final_response])
        mock_adapter.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": []})
        mock_adapter.build_tool_result_message = MagicMock(return_value={"role": "user", "content": []})

        db = AsyncMock(spec=AsyncSession)
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        with (
            patch.object(settings, "MULTI_AGENT_ENABLED", False),
            patch(f"{_ORCH}.get_tenant_ai_config", new_callable=AsyncMock, return_value=_DEFAULT_AI_CONFIG),
            patch(f"{_ORCH}.get_adapter", return_value=mock_adapter),
            patch(f"{_ORCH}.retriever_node", new_callable=AsyncMock),
            patch(
                f"{_ORCH}.build_all_tool_definitions",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "name": "data_sample_table_read",
                        "description": "Read",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ],
            ),
            patch(
                f"{_ORCH}.execute_tool_call",
                new_callable=AsyncMock,
                return_value='{"data": []}',
            ),
            patch(f"{_ORCH}.log_event", new_callable=AsyncMock),
            patch(f"{_ORCH}.get_active_template", new_callable=AsyncMock, return_value="You are a helpful assistant."),
            patch(f"{_ORCH}.deduct_chat_credits", new_callable=AsyncMock, return_value=None),
            patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
        ):
            result = await _collect_stream_result(
                run_chat_turn(
                    db=db,
                    session=session,
                    user_message="Keep trying",
                    user_id=user_id,
                    tenant_id=tenant_id,
                )
            )

        assert result["content"] == "I've exhausted my tool calls."


class TestAgenticAllowlistEnforcement:
    """Test that disallowed tools return error results."""

    @pytest.mark.asyncio
    async def test_disallowed_tool_returns_error_result(self):
        """If the LLM tries to call a disallowed tool, execute_tool_call returns an error."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        session = _make_session(tenant_id)

        tool_response = _make_llm_response(
            tool_blocks=[
                ToolUseBlock(
                    id="tool_1",
                    name="schedule_create",
                    input={"name": "bad"},
                )
            ],
        )
        text_response = _make_llm_response(text="Sorry, I can't do that.")

        mock_adapter = MagicMock()
        mock_adapter.create_message = AsyncMock(side_effect=[tool_response, text_response])
        mock_adapter.stream_message = _make_stream_side_effect([tool_response, text_response])
        mock_adapter.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": []})
        mock_adapter.build_tool_result_message = MagicMock(return_value={"role": "user", "content": []})

        db = AsyncMock(spec=AsyncSession)
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        with (
            patch.object(settings, "MULTI_AGENT_ENABLED", False),
            patch(f"{_ORCH}.get_tenant_ai_config", new_callable=AsyncMock, return_value=_DEFAULT_AI_CONFIG),
            patch(f"{_ORCH}.get_adapter", return_value=mock_adapter),
            patch(f"{_ORCH}.retriever_node", new_callable=AsyncMock),
            patch(f"{_ORCH}.build_all_tool_definitions", new_callable=AsyncMock, return_value=[]),
            patch(f"{_ORCH}.log_event", new_callable=AsyncMock),
            patch(f"{_ORCH}.get_active_template", new_callable=AsyncMock, return_value="You are a helpful assistant."),
            patch(f"{_ORCH}.deduct_chat_credits", new_callable=AsyncMock, return_value=None),
            patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
        ):
            result = await _collect_stream_result(
                run_chat_turn(
                    db=db,
                    session=session,
                    user_message="Create a schedule",
                    user_id=user_id,
                    tenant_id=tenant_id,
                )
            )

        assert result["content"] == "Sorry, I can't do that."
        assert result["tool_calls"] is not None
        assert "not allowed" in result["tool_calls"][0]["result_summary"]
