# backend/tests/services/chat/test_metric_nonstreaming_run_suppression.py
"""Anti-hallucination invariant on the NON-STREAMING agent path.

The metric trust boundary (withhold the computed number from the LLM, render it
on the FE) was wired ONLY into the streaming interceptor
(``run_streaming(tool_result_interceptor=...)`` → ``_intercept_tool_result``).
The non-streaming ``BaseSpecialistAgent.run()`` path (used by
``UnifiedAgent.run`` / ``SuiteQLAgent.run`` and reachable in production) has NO
interceptor: it appends the raw ``execute_tool_call`` result string — INCLUDING
the metric's ``rows`` with the literal computed number — straight into the
tool_result message handed to the LLM.

The prior tests were vacuous about this seam: ``test_metric_interception.py``
only exercised the streaming-side ``_intercept_tool_result`` in isolation, so a
metric computed on the non-streaming path leaked its number to the LLM and every
existing test still passed.

This test drives the REAL ``run()`` loop with a mock adapter that asks for
``metric_compute``; ``execute_tool_call`` returns a real metric ``data_table``
payload (``suppress_llm_value: True``). We then inspect the content the agent
actually hands to the LLM (the captured ``tool_results_content``) and assert the
computed number is ABSENT — while a normal SuiteQL data_table on the same path
is left byte-identical (rule: suppression is opt-in; non-metric tools unchanged).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chat.agents.base_agent import BaseSpecialistAgent
from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock
from app.services.metrics.metric_compute import metric_data_table

_LEAKED_VALUE = 1234567.89


class _ToolAgent(BaseSpecialistAgent):
    agent_name = "test"
    max_steps = 3

    @property
    def system_prompt(self):
        return "test prompt"

    @property
    def tool_definitions(self):
        return [{"name": "metric_compute", "description": "x", "input_schema": {"type": "object", "properties": {}}}]


def _build_agent() -> _ToolAgent:
    agent = _ToolAgent.__new__(_ToolAgent)
    agent.tenant_id = None
    agent.user_id = None
    agent.correlation_id = "test"
    return agent


def _mock_adapter(tool_name: str):
    """Adapter that returns ONE tool call then a final text answer. Captures the
    tool_result message content via build_tool_result_message so the test can
    inspect exactly what the LLM was handed."""
    adapter = MagicMock()
    tool_response = LLMResponse(
        text_blocks=[],
        tool_use_blocks=[ToolUseBlock(id="t1", name=tool_name, input={"key": "gross_revenue"})],
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )
    text_response = LLMResponse(
        text_blocks=["done"],
        tool_use_blocks=[],
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )
    adapter.create_message = AsyncMock(side_effect=[tool_response, text_response])
    adapter.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": []})

    captured: dict = {}

    def _build_tool_result_message(tool_results_content):
        captured["content"] = tool_results_content
        return {"role": "user", "content": tool_results_content}

    adapter.build_tool_result_message = MagicMock(side_effect=_build_tool_result_message)
    return adapter, captured


def _llm_facing_text(captured: dict) -> str:
    return json.dumps(captured.get("content", []))


@pytest.mark.asyncio
async def test_nonstreaming_run_withholds_metric_number_from_llm():
    """THE invariant: a metric computed on the non-streaming run() path must NOT
    hand its number to the LLM. Fails pre-fix because run() has no interceptor."""
    agent = _build_agent()
    adapter, captured = _mock_adapter("metric_compute")

    # The REAL metric payload the compute tool returns (suppress_llm_value=True).
    metric_payload = metric_data_table(
        "Gross Revenue", _LEAKED_VALUE, "currency", "this_month", {"query": "SELECT 0", "dialect": "suiteql"}
    )

    with (
        patch("app.services.chat.tools.execute_tool_call", new_callable=AsyncMock) as mock_exec,
        patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", new_callable=AsyncMock),
        patch("app.services.chat.agents.base_agent.extract_structured_confidence", new_callable=AsyncMock) as mock_conf,
        patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
    ):
        mock_exec.return_value = json.dumps(metric_payload, default=str)
        mock_conf.return_value = MagicMock(score=4, source="mock")

        await agent.run(task="what is gross revenue", context={}, db=AsyncMock(), adapter=adapter, model="m")

    llm_text = _llm_facing_text(captured)
    # The computed number must NOT be present in ANY form in the LLM-facing content.
    assert str(_LEAKED_VALUE) not in llm_text, (
        "metric value leaked to the LLM on the non-streaming run() path "
        "(anti-hallucination breach): the suppression is wired only in the streaming interceptor"
    )
    assert "1234567" not in llm_text
    # The raw rows array must not be handed over either.
    parsed_content = captured["content"][0]["content"]
    parsed = json.loads(parsed_content)
    assert "rows" not in parsed
    # ...but the shape/commentary note still reaches the LLM so it can respond.
    assert parsed.get("row_count") == 1
    assert "note" in parsed


@pytest.mark.asyncio
async def test_nonstreaming_run_leaves_non_metric_data_table_untouched():
    """Rule guard: suppression is opt-in. A normal SuiteQL data_table (no
    suppress_llm_value flag) must reach the LLM byte-identical on run() — we must
    NOT start condensing every tool result on the non-streaming path."""
    agent = _build_agent()
    adapter, captured = _mock_adapter("netsuite_suiteql")

    suiteql_payload = {
        "columns": ["order_id", "amount"],
        "rows": [["SO-1", 100.0], ["SO-2", 250.5]],
        "row_count": 2,
        "query": "SELECT order_id, amount FROM transaction",
        "truncated": False,
    }
    raw = json.dumps(suiteql_payload, default=str)

    with (
        patch("app.services.chat.tools.execute_tool_call", new_callable=AsyncMock) as mock_exec,
        patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", new_callable=AsyncMock),
        patch("app.services.chat.agents.base_agent.extract_structured_confidence", new_callable=AsyncMock) as mock_conf,
        patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
    ):
        mock_exec.return_value = raw
        mock_conf.return_value = MagicMock(score=4, source="mock")

        await agent.run(task="list orders", context={}, db=AsyncMock(), adapter=adapter, model="m")

    content = captured["content"][0]["content"]
    parsed = json.loads(content)
    # Byte-identical: the SuiteQL rows (and their values) are still present.
    assert parsed["rows"] == [["SO-1", 100.0], ["SO-2", 250.5]]
    assert "100.0" in content
