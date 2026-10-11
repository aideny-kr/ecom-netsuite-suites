"""Data-query retries must not discard ordinary saved-evidence summaries."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services.chat.agents.base_agent import BaseSpecialistAgent, _task_contains_query
from app.services.chat.llm_adapter import LLMResponse


@pytest.mark.parametrize(
    "task",
    [
        "Summarize the returned partial documentation evidence in two sentences with the official clickable "
        "source link from the result. Use the evidence already in this conversation; do not call any tools "
        "or create workflows.",
        "Give me a summary of the documentation.",
        "Explain the assumptions in this paragraph.",
        "Explain accounting terminology.",
    ],
)
def test_ordinary_words_do_not_trigger_data_query_retry(task):
    assert not _task_contains_query(task)


@pytest.mark.parametrize(
    "task",
    [
        "SELECT id FROM transaction",
        "How many invoices are open?",
        "What is the total revenue?",
        "Compare the totals and averages.",
        "Count unpaid invoices.",
        "Return invoice counts.",
        "Sum the quantities.",
        "Return the sums by department.",
        "Show average order quantity.",
        "Show sales, orders and inventory.",
        "Summarize sales this month.",
    ],
)
def test_explicit_data_questions_still_require_execution(task):
    assert _task_contains_query(task)


class _GuardAgent(BaseSpecialistAgent):
    agent_name = "query-guard-test"
    max_steps = 2
    system_prompt = "Answer using the supplied documentation evidence."
    tool_definitions = []


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "task,expected_calls",
    [("Summarize the returned documentation evidence.", 1), ("What is the total revenue?", 2)],
)
async def test_query_guard_only_retries_actual_data_questions(streaming, task, expected_calls):
    agent = _GuardAgent(uuid4(), uuid4(), "query-guard-test")
    answer = "The documentation describes Python functions. Source: https://learn.microsoft.com/"
    response = LLMResponse(text_blocks=[answer])
    adapter = MagicMock()
    adapter.create_message = AsyncMock(return_value=response)
    adapter.build_assistant_message.return_value = {"role": "assistant", "content": answer}
    stream_calls = []

    async def stream_message(**kwargs):
        stream_calls.append(kwargs)
        yield "text", answer
        yield "response", response

    adapter.stream_message = stream_message
    with (
        patch("app.services.policy_service.get_active_policy", AsyncMock(return_value=None)),
        patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", AsyncMock()),
        patch(
            "app.services.chat.agents.base_agent.extract_structured_confidence",
            AsyncMock(return_value=MagicMock(score=5, source="test")),
        ),
    ):
        kwargs = {"task": task, "context": {}, "db": AsyncMock(), "adapter": adapter, "model": "test-model"}
        if streaming:
            events = [event async for event in agent.run_streaming(**kwargs)]
            result = next(payload for kind, payload in events if kind == "response")
            assert len(stream_calls) == expected_calls
        else:
            result = await agent.run(**kwargs)
            assert adapter.create_message.await_count == expected_calls
    assert result.data == answer
    assert result.tool_calls_log == []
