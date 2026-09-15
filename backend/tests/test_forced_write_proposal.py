"""Regressions for unnecessary model hops and inferred write intent.

Metadata is evidence, not a request to mutate. Actual volunteered writes still
pass the same schema, slot and HITL pipeline exercised by the approval suites.
"""

from unittest.mock import AsyncMock

import pytest

from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock
from tests.test_write_investigation_gate import _ext, _make_adapter, _make_agent, _patches, _run


def answer(text="Explanation based on the supplied context."):
    return LLMResponse(text_blocks=[text], usage=TokenUsage(10, 20, 30, 40))


@pytest.mark.parametrize(
    "task",
    [
        "Explain the difference between sales and revenue.",
        "What does SELECT mean in SQL?",
        "Which BigQuery dataset should I select for order counts?",
        "Summarize the inventory investigation plan; don't query anything.",
        "Explain the total in the result we just fetched.",
    ],
)
async def test_keywords_do_not_override_a_legitimate_answer_or_clarification(task):
    agent = _make_agent()
    adapter = _make_adapter([answer()])
    adapter.create_message = AsyncMock(side_effect=AssertionError("Unexpected model hop"))
    with _patches():
        events = await _run(agent, adapter, task)
    result = next(p for t, p in events if t == "response")
    assert result.success
    assert result.tool_calls_log == []
    assert result.tokens_used == TokenUsage(10, 20, 30, 40)
    assert not [p for t, p in events if t == "confirmation_required"]
    adapter.create_message.assert_not_awaited()


async def test_metadata_question_finishes_without_a_retry_or_manufactured_card():
    response = answer("The retrieved customer schema lists subsidiary and company name.")
    lookup = LLMResponse(
        tool_use_blocks=[ToolUseBlock("schema", _ext("ns_getRecordTypeMetadata"), {"recordType": "customer"})],
        usage=TokenUsage(1, 2, 3, 4),
    )
    adapter = _make_adapter([lookup, response])
    adapter.create_message = AsyncMock(side_effect=AssertionError("Unexpected model hop"))
    with _patches():
        events = await _run(_make_agent(), adapter, "What fields does a customer record require?")
    result = next(p for t, p in events if t == "response")
    assert result.success and result.data == response.text_blocks[0]
    assert len(result.tool_calls_log) == 1
    assert result.tokens_used == TokenUsage(11, 22, 33, 44)
    assert not [p for t, p in events if t == "confirmation_required"]
    adapter.create_message.assert_not_awaited()


async def test_nonstreaming_explanation_is_not_forced_into_a_data_query():
    agent = _make_agent()
    adapter = _make_adapter([])
    adapter.create_message = AsyncMock(return_value=answer())
    with _patches():
        result = await agent.run(
            task="Explain sales tax terminology", context={}, db=AsyncMock(), adapter=adapter, model="test-model"
        )
    assert result.success
    assert result.tool_calls_log == []
    assert adapter.create_message.await_count == 1
    assert result.tokens_used == TokenUsage(10, 20, 30, 40)
