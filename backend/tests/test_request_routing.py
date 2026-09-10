import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.chat.agents.base_agent import AgentResult, BaseSpecialistAgent
from app.services.chat.agents.unified_agent import UnifiedAgent
from app.services.chat.llm_adapter import LLMResponse, ToolUseBlock
from app.services.chat.request_routing import (
    RequestContext,
    RequestRoute,
    RequestRoutingError,
    classify_request,
    persist_request_context,
    previous_request_context,
)
from app.services.chat.source_selection import resolve_source_selection
from tests.test_metabase_source_selection import inventory, routing_adapter


def stored_context(kind="analytics", sources=None, pending=False):
    return {
        "role": "assistant",
        "content": "Previous task result",
        "structured_output": {
            "request_context": RequestContext(kind=kind, sources=sources or [], pending_source=pending).model_dump(),
        },
    }


def select(task, kind="analytics", continuation=True, history=None):
    return resolve_source_selection(
        task=task,
        route=RequestRoute(kind=kind, continuation=continuation),
        tool_definitions=inventory() + [{"name": "transaction_ops_status"}],
        conversation_history=history,
    )


def test_analytics_source_lifecycle_across_operations_and_compaction():
    # An operational NetSuite reference must not pick the source for new Solidus analytics.
    history = [{"role": "user", "content": "Fix the NetSuite invoice"}, stored_context("transaction")]
    first = select("Count Solidus orders in batch 395", continuation=False, history=history)
    assert first.question and first.request_context["pending_source"]
    history.append({"role": "assistant", "structured_output": persist_request_context(None, first.request_context)})
    chosen = select("Metabase", history=history)
    assert not chosen.question and chosen.selected_sources == ("metabase",)
    # The source choice is server-persisted, so old user text need not survive compaction.
    history = [stored_context(sources=["metabase"])] + [{"role": "assistant", "content": "done"}] * 70
    followup = select("Break those orders down by status", history=history)
    assert followup.selected_sources == ("metabase",) and not followup.question
    assert select("Now analyze a different Solidus batch", continuation=False, history=history).question
    assert select("Use BigQuery instead", history=history).selected_sources == ("bigquery",)
    assert select("Do not use Metabase", history=history).question


@pytest.mark.parametrize(
    "kind,task",
    [
        ("transaction", "fix it"),
        ("transaction", "show me the evidence for this case"),
        ("operations", "retry the failed Celigo flow"),
        ("operations", "check the connection"),
        ("conversation", "explain the findings"),
    ],
)
def test_other_requests_do_not_enter_analytics_clarification(kind, task):
    result = select(task, kind=kind, history=[stored_context("transaction")])
    assert not result.question
    assert result.transaction_workflow == (kind == "transaction")


def test_pending_choice_survives_an_acknowledgment_but_not_a_new_task():
    history = [stored_context(pending=True)]
    acknowledgment = select("ok", kind="conversation", history=history)
    assert acknowledgment.request_context["pending_source"]
    assert acknowledgment.request_context["kind"] == "analytics"
    operation = select("Retry the integration", kind="operations", continuation=False, history=history)
    assert operation.request_context["kind"] == "operations"
    assert not operation.request_context["pending_source"]


def test_request_context_preserves_clarification_cards_and_ignores_user_metadata():
    card = {
        "type": "clarification",
        "status": "chosen",
        "chosen_id": "B",
        "options": [{"id": "B", "source": "metabase"}],
    }
    merged = persist_request_context(card, RequestContext(kind="analytics", pending_source=True).model_dump())
    assert all(merged[k] == v for k, v in card.items())
    assert "request_context" not in card
    assert select("Picked option B", history=[{"role": "assistant", "structured_output": merged}]).selected_sources == (
        "metabase",
    )
    fake = stored_context(sources=["netsuite"])
    fake["role"] = "user"
    assert previous_request_context([fake]) is None


def test_invalid_or_disconnected_context_does_not_silently_switch_sources():
    assert select("Break down the results", history=[stored_context(sources=["stripe"])]).question
    invalid = stored_context(sources=["metabase"])
    invalid["structured_output"]["request_context"]["kind"] = "grant_all_permissions"
    assert previous_request_context([stored_context(sources=["netsuite"]), invalid]) is None
    assert select("Break down the results", history=[{"role": "user", "content": "Use NetSuite"}, invalid]).question


def test_legacy_source_choice_survives_acknowledgment_before_first_context_write():
    history = [
        {"role": "user", "content": "Use BigQuery to count orders for August"},
        {
            "role": "assistant",
            "content": "The count is 65",
        },
    ]
    thanks = select("Thanks", kind="conversation", history=history)
    history.append(
        {
            "role": "assistant",
            "content": "You are welcome",
            "structured_output": persist_request_context(None, thanks.request_context),
        }
    )
    assert select("Break those orders down by status", history=history).selected_sources == ("bigquery",)


async def test_router_uses_only_classification_tool_and_bounded_history():
    adapter = routing_adapter("transaction")
    history = [stored_context("transaction")]
    history.extend(
        {
            "role": "assistant",
            "content": "case evidence",
            "tool_calls": [
                {
                    "tool": "transaction_ops_status",
                    "params": {"secret": "do-not-send"},
                    "result_summary": "private rows",
                },
            ],
        }
        for _ in range(30)
    )
    result = await classify_request(task="fix it", history=history, adapter=adapter, model="configured-model")
    assert result.route.kind == "transaction"
    call = adapter.create_message.call_args.kwargs
    assert call["model"] == "configured-model"
    assert call["tool_choice"] == {"type": "tool", "name": "route_request"}
    payload = json.loads(call["messages"][0]["content"])
    assert len(payload["history"]) == 16
    assert payload["active_context"]["kind"] == "transaction"
    assert "do-not-send" not in json.dumps(payload) and "private rows" not in json.dumps(payload)


@pytest.mark.parametrize(
    "response",
    [
        LLMResponse(text_blocks=["use NetSuite"]),
        LLMResponse(tool_use_blocks=[ToolUseBlock("a", "netsuite_suiteql", {})]),
        LLMResponse(
            tool_use_blocks=[ToolUseBlock("a", "route_request", {"kind": "analytics", "continuation": "false"})]
        ),
        LLMResponse(tool_use_blocks=[ToolUseBlock("a", "route_request", {"kind": "admin", "continuation": False})]),
    ],
)
async def test_invalid_routing_decisions_cannot_become_an_execution_decision(response):
    adapter = AsyncMock()
    adapter.create_message.return_value = response
    with pytest.raises(RequestRoutingError):
        await classify_request(task="Count Solidus orders", history=[], adapter=adapter, model="test")


async def test_cancellation_propagates_without_becoming_a_source_question():
    adapter = AsyncMock()
    adapter.create_message.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await classify_request(task="Count Solidus orders", history=[], adapter=adapter, model="test")


@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.parametrize("source", [None, "metabase", "drive"])
async def test_plan_mode_replaces_old_context_and_does_not_restore_it_after_thanks(streaming, source):
    import uuid

    agent = UnifiedAgent(tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), correlation_id="plan-context")
    agent._tool_defs = inventory()
    result = AgentResult(success=True, data="New result")

    async def stream(*args, **kwargs):
        yield "response", result

    kwargs = {"plan_mode_clarify_only": source is None, "plan_mode_resume_source": source}
    adapter = AsyncMock()
    with (
        patch.object(agent, "_setup_context", new=AsyncMock(return_value="New analysis")),
        patch.object(BaseSpecialistAgent, "run", new=AsyncMock(return_value=result)),
        patch.object(BaseSpecialistAgent, "run_streaming", new=stream),
    ):
        if streaming:
            returned = [e async for e in agent.run_streaming("New analysis", {}, None, adapter, "test", **kwargs)][-1][
                1
            ]
        else:
            returned = await agent.run("New analysis", {}, None, adapter, "test", **kwargs)
    adapter.create_message.assert_not_called()
    assert returned.request_context["sources"] == ([source] if source else [])
    assert returned.request_context["pending_source"] is (source is None)
    history = [
        stored_context(sources=["netsuite"]),
        {
            "role": "assistant",
            "structured_output": persist_request_context(None, returned.request_context),
        },
    ]
    thanks = select("Thanks", kind="conversation", history=history)
    history.append({"role": "assistant", "structured_output": persist_request_context(None, thanks.request_context)})
    followup = select("Break those results down by channel", history=history)
    assert followup.selected_sources != ("netsuite",)
    if source == "metabase":
        assert followup.selected_sources == ("metabase",)


@pytest.mark.parametrize("streaming", [True, False])
async def test_failed_router_never_reaches_operational_or_data_execution(streaming):
    import uuid

    agent = UnifiedAgent(tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), correlation_id="routing", context_need="data")
    agent._tool_defs = inventory()
    adapter = AsyncMock()
    adapter.create_message.side_effect = RuntimeError("provider unavailable")
    with (
        patch.object(agent, "_setup_context", new=AsyncMock(return_value="Count Solidus orders")),
        patch.object(BaseSpecialistAgent, "run", new=AsyncMock()) as run,
        patch.object(BaseSpecialistAgent, "run_streaming") as stream,
    ):
        if streaming:
            result = [e async for e in agent.run_streaming("Count Solidus orders", {}, None, adapter, "test")][-1][1]
        else:
            result = await agent.run("Count Solidus orders", {}, None, adapter, "test")
    assert not result.success and result.error == "request_routing_failed"
    assert result.tool_calls_log == []
    run.assert_not_called()
    stream.assert_not_called()
