# backend/tests/services/chat/test_metric_streaming_run_suppression.py
"""Anti-hallucination invariant on the STREAMING agent path with NO interceptor.

The metric trust boundary (withhold the computed number from the LLM, render it
on the FE) is wired into ``run_streaming`` ONLY via the optional
``tool_result_interceptor`` callback. When that callback is absent — which is the
case for the vs-MCP benchmark runner (``benchmarks/agent_runner.py``) and any
caller that drives ``run_streaming`` without wiring the orchestrator's
interceptor — ``llm_result_str`` defaults to the raw ``result_str``, so a
``metric_compute`` result hands its ``rows`` (the literal computed number)
straight to the LLM. That leak reaches the north-star CI gate.

The non-streaming ``run()`` path was already hardened with
``_suppress_metric_value_for_llm`` (TOOL-enforced, interceptor-independent). This
test pins the SAME invariant on the streaming path: with NO interceptor, a metric
data_table (``suppress_llm_value: True``) must NOT leak its number to the LLM,
while a normal SuiteQL data_table on the same path is left byte-identical
(suppression is opt-in; non-metric tools unchanged).

This test FAILS pre-fix: ``run_streaming`` has no value-suppression guard when
``tool_result_interceptor is None``.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chat.agents.base_agent import BaseSpecialistAgent
from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock
from app.services.metrics.metric_compute import metric_data_table

_LEAKED_VALUE = 0.2531


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


def _mock_streaming_adapter(tool_name: str):
    """Adapter whose ``stream_message`` yields ONE tool call on step 0 then a final
    text answer on step 1. Captures the tool_result message content via
    ``build_tool_result_message`` so the test can inspect exactly what the LLM was
    handed."""
    adapter = MagicMock()

    tool_response = LLMResponse(
        text_blocks=[],
        tool_use_blocks=[ToolUseBlock(id="t1", name=tool_name, input={"key": "net_margin"})],
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )
    text_response = LLMResponse(
        text_blocks=["done"],
        tool_use_blocks=[],
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )
    responses = [tool_response, text_response]
    call_index = {"i": 0}

    async def _stream_message(*args, **kwargs):
        resp = responses[call_index["i"]]
        call_index["i"] += 1
        # stream_message yields ("text", chunk) events then a terminal ("response", resp)
        for blk in resp.text_blocks:
            yield "text", blk
        yield "response", resp

    adapter.stream_message = _stream_message
    adapter.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": []})

    captured: dict = {}

    def _build_tool_result_message(tool_results_content):
        captured["content"] = tool_results_content
        return {"role": "user", "content": tool_results_content}

    adapter.build_tool_result_message = MagicMock(side_effect=_build_tool_result_message)
    return adapter, captured


def _llm_facing_text(captured: dict) -> str:
    return json.dumps(captured.get("content", []))


async def _drive_stream(agent, adapter):
    """Exhaust the run_streaming generator with NO tool_result_interceptor."""
    async for _event in agent.run_streaming(
        task="what is net margin",
        context={},
        db=AsyncMock(),
        adapter=adapter,
        model="m",
        tool_result_interceptor=None,
    ):
        pass


@pytest.mark.asyncio
async def test_streaming_run_withholds_metric_number_from_llm_without_interceptor():
    """THE invariant: a metric computed on the streaming path with NO interceptor
    must NOT hand its number to the LLM. Fails pre-fix because run_streaming only
    suppresses when an interceptor is wired (the benchmark runner wires none)."""
    agent = _build_agent()
    adapter, captured = _mock_streaming_adapter("metric_compute")

    # The REAL metric payload the compute tool returns (suppress_llm_value=True).
    metric_payload = metric_data_table(
        "Net Margin", _LEAKED_VALUE, "ratio", "this_month", {"query": "SELECT 0", "dialect": "suiteql"}
    )

    with (
        patch("app.services.chat.tools.execute_tool_call", new_callable=AsyncMock) as mock_exec,
        patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", new_callable=AsyncMock),
        patch(
            "app.services.chat.agents.base_agent.extract_structured_confidence",
            new_callable=AsyncMock,
        ) as mock_conf,
        patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
    ):
        mock_exec.return_value = json.dumps(metric_payload, default=str)
        mock_conf.return_value = MagicMock(score=4, source="mock")

        await _drive_stream(agent, adapter)

    llm_text = _llm_facing_text(captured)
    # The computed number must NOT be present in ANY form in the LLM-facing content.
    assert str(_LEAKED_VALUE) not in llm_text, (
        "metric value leaked to the LLM on the streaming path with NO interceptor "
        "(anti-hallucination breach): suppression is wired only via the optional interceptor, "
        "so the vs-MCP benchmark runner leaks the number into the north-star CI gate"
    )
    assert "0.2531" not in llm_text
    # The raw rows array must not be handed over either.
    parsed_content = captured["content"][0]["content"]
    parsed = json.loads(parsed_content)
    assert "rows" not in parsed
    # ...but the shape/commentary note still reaches the LLM so it can respond.
    assert parsed.get("row_count") == 1
    assert "note" in parsed


@pytest.mark.asyncio
async def test_streaming_run_leaves_non_metric_data_table_untouched_without_interceptor():
    """Rule guard: suppression is opt-in. A normal SuiteQL data_table (no
    suppress_llm_value flag) must reach the LLM byte-identical on the streaming
    path — we must NOT start condensing every tool result when no interceptor is
    wired."""
    agent = _build_agent()
    adapter, captured = _mock_streaming_adapter("netsuite_suiteql")

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
        patch(
            "app.services.chat.agents.base_agent.extract_structured_confidence",
            new_callable=AsyncMock,
        ) as mock_conf,
        patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
    ):
        mock_exec.return_value = raw
        mock_conf.return_value = MagicMock(score=4, source="mock")

        await _drive_stream(agent, adapter)

    content = captured["content"][0]["content"]
    parsed = json.loads(content)
    # Byte-identical: the SuiteQL rows (and their values) are still present.
    assert parsed["rows"] == [["SO-1", 100.0], ["SO-2", 250.5]]
    assert "100.0" in content
