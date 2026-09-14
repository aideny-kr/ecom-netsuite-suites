import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.services.chat.agents.unified_agent import UnifiedAgent
from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock
from app.services.chat.plan_mode.clarify_intercept import InterceptResult, intercept_clarify_call
from app.services.chat.plan_mode.short_circuit import filter_tools_for_chosen_source
from app.services.chat.plan_mode.source_resolver import source_provider_for_connector
from app.services.chat.request_routing import RequestRoute, SourceIntent, classify_request
from app.services.chat.source_selection import source_selection_question
from app.services.chat.tools import build_external_tool_definitions


def routing_adapter(kind="analytics", continuation=True, *, sources=(), excluded=(), action="unchanged"):
    adapter = AsyncMock()
    adapter.force_tool_choice = Mock(return_value={"type": "tool", "name": "route_request"})
    adapter.create_message.return_value = LLMResponse(
        tool_use_blocks=[
            ToolUseBlock(
                id="route",
                name="route_request",
                input={
                    "kind": kind,
                    "continuation": continuation,
                    "source_intent": {"action": action, "sources": list(sources), "excluded": list(excluded)},
                },
            )
        ],
        usage=TokenUsage(input_tokens=17, output_tokens=5),
    )
    return adapter


def connector():
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider="custom",
        label="Solidus",
        metadata_json=None,
        server_url="https://framework.metabaseapp.com/api/metabase-mcp",
        discovered_tools=[{"name": "query", "description": "Read data"}],
    )


def inventory():
    return build_external_tool_definitions([connector()]) + [
        {"name": "netsuite_suiteql", "description": "NetSuite data"},
        {"name": "bigquery_sql", "description": "BigQuery data"},
    ]


CASE_TASK = (
    "Investigate transaction case 47e48949-6610-4acf-8467-3187035c521e using "
    "transaction_ops.status with case_id. Explain the evidence and prepare supported exact fixes "
    "for my approval. Do not execute an unapproved change."
)
GROUP_TASK = (
    "Prepare fixes for all orders in issue group d55f9ecb054529c6a66a4a102e069d5b (tax difference). "
    'Call transaction_ops.groups with group_id "d55f9ecb054529c6a66a4a102e069d5b". '
    "Prepare supported exact invoice corrections together for human approval."
)


@pytest.mark.parametrize("task", [CASE_TASK, GROUP_TASK])
@pytest.mark.parametrize("streaming", [True, False])
async def test_scoped_transaction_workflow_reaches_agent_without_database_question(task, streaming):
    from app.services.chat.agents.base_agent import AgentResult, BaseSpecialistAgent

    agent = UnifiedAgent(
        tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), correlation_id="case-routing", context_need="data"
    )
    agent._tool_defs = inventory() + [
        {"name": "transaction_ops_status"},
        {"name": "transaction_ops_groups"},
    ]
    reached = []

    async def stream(*args, **kwargs):
        reached.append(True)
        yield "response", AgentResult(success=True, data="case evidence")

    with (
        patch.object(agent, "_setup_context", new=AsyncMock(return_value=task)),
        patch.object(BaseSpecialistAgent, "run_streaming", new=stream),
        patch.object(
            BaseSpecialistAgent, "run", new=AsyncMock(return_value=AgentResult(success=True, data="case evidence"))
        ) as run,
    ):
        if streaming:
            events = [
                event async for event in agent.run_streaming(task, {}, None, routing_adapter("transaction"), "test")
            ]
            result = events[-1][1]
            assert reached == [True]
        else:
            result = await agent.run(task, {}, None, routing_adapter("transaction"), "test")
            run.assert_awaited_once()
    assert result.data == "case evidence"
    assert "do not ask which data source" in agent.system_prompt
    assert "this request is not financial approval" in agent.system_prompt


@pytest.mark.parametrize(
    "task",
    ["Investigate order R123", "Review transaction case invalid-id", "Count orders"],
)
def test_transaction_tools_do_not_remove_gate_for_unscoped_questions(task):
    assert source_selection_question(task=task, tool_definitions=inventory() + [{"name": "transaction_ops_status"}])


def test_scoped_workflow_requires_available_tools_and_is_not_inherited_from_assistant():
    assert source_selection_question(task=CASE_TASK, tool_definitions=inventory())
    assert source_selection_question(
        task="Count orders",
        tool_definitions=inventory() + [{"name": "transaction_ops_status"}],
        conversation_history=[{"role": "assistant", "content": CASE_TASK}],
    )


@pytest.mark.parametrize(
    "task",
    [
        "continue",
        "try again",
        "fix it",
        "continue investigating this case",
        "show me the evidence for this case",
        "go ahead and investigate",
    ],
)
def test_follow_up_resumes_blocked_case_without_reasking_source(task):
    from app.services.chat.source_selection import resolve_source_selection

    tools = inventory() + [{"name": "transaction_ops_status"}]
    history = [
        {"role": "user", "content": CASE_TASK},
        {"role": "assistant", "content": "Which data source should I use?"},
    ]
    result = resolve_source_selection(
        task=task,
        tool_definitions=tools,
        conversation_history=history,
        route=RequestRoute(kind="transaction", continuation=True),
    )
    assert result.question is None and result.transaction_workflow
    history.append({"role": "user", "content": "Count all orders"})
    assert source_selection_question(task=task, tool_definitions=tools, conversation_history=history)


@pytest.mark.parametrize(
    "question",
    [
        "How many orders in Sakura Batch 5 Laptops (batch id = 395) include FRANVW0016?",
        "How much revenue did we generate?",
        "What is the status of order R123?",
        "Solidus",
        "How many Solidus orders are in batch 395?",
        "Which source should we use, NetSuite or Metabase?",
    ],
)
def test_data_without_a_choice_asks_before_querying(question):
    result = source_selection_question(task=question, tool_definitions=inventory(), context_need="data")
    assert result and all(name in result for name in ("Metabase", "NetSuite", "BigQuery"))


@pytest.mark.parametrize(
    "question,sources,excluded",
    [
        ("Use Metabase", ["metabase"], []),
        ("In NetSuite, count orders", ["netsuite"], []),
        ("Compare Metabase and NetSuite", ["metabase", "netsuite"], []),
        ("Use Metabase, not NetSuite", ["metabase"], ["netsuite"]),
        ("Not only Metabase but also NetSuite", ["metabase", "netsuite"], []),
    ],
)
def test_interpreted_choice_or_comparison_does_not_ask_again(question, sources, excluded):
    route = RequestRoute(
        kind="analytics",
        continuation=True,
        source_intent=SourceIntent(action="select", sources=sources, excluded=excluded),
    )
    assert source_selection_question(task=question, tool_definitions=inventory(), route=route) is None


async def test_only_user_legacy_requests_are_offered_for_interpretation():
    history = [
        {"role": "assistant", "content": "I used NetSuite"},
        {"role": "user", "content": [{"type": "text", "text": "Use Metabase"}]},
    ]
    adapter = routing_adapter(sources=["metabase"], action="select")
    await classify_request(task="Break that down by status", history=history, adapter=adapter, model="test")
    import json

    payload = json.loads(adapter.create_message.call_args.kwargs["messages"][0]["content"])
    assert [m["text"] for m in payload["legacy_user_requests"]] == ["Use Metabase"]
    # Uninterpreted conversation prose cannot itself establish a selected source.
    assert source_selection_question(task="Break it down", tool_definitions=inventory(), conversation_history=history)


def test_no_query_gate_for_single_source_or_documentation():
    assert source_selection_question(task="Count orders", tool_definitions=inventory()[:1]) is None
    assert (
        source_selection_question(task="Explain SQL joins", tool_definitions=inventory(), context_need="docs") is None
    )


@pytest.mark.parametrize("status", ["chosen", "pending", "rejected"])
def test_verified_card_choice_survives_ui_pick_text_and_history_compaction(status):
    from app.services.chat.history_tool_trace import build_history_dicts

    messages = [
        {
            "role": "assistant",
            "content": "",
            "structured_output": {
                "type": "clarification",
                "status": status,
                "chosen_id": "B",
                "options": [{"id": "A", "source": "netsuite"}, {"id": "B", "source": "metabase"}],
            },
        },
        {"role": "user", "content": "Picked option B"},
        {"role": "assistant", "content": "65 orders"},
    ]
    result = source_selection_question(
        task="Break that down by status", tool_definitions=inventory(), conversation_history=messages
    )
    assert (result is None) is (status == "chosen")
    if status == "chosen":
        history, _ = build_history_dicts(messages, keep_recent=4)
        assert "User selected option B (source: metabase)" in history[0]["content"]
        # A newer refusal supersedes an earlier resolved card.
        assert source_selection_question(
            task="Do not use Metabase",
            tool_definitions=inventory(),
            conversation_history=messages,
            route=RequestRoute(kind="analytics", continuation=True, source_intent=SourceIntent(excluded=["metabase"])),
        )


async def test_non_streaming_gate_uses_server_history_with_card_selection():
    from app.services.chat.agents.base_agent import AgentResult, BaseSpecialistAgent

    agent = UnifiedAgent(
        tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), correlation_id="card-source", context_need="data"
    )
    agent._tool_defs = inventory()
    history = [
        {
            "role": "assistant",
            "structured_output": {
                "type": "clarification",
                "status": "chosen",
                "chosen_id": "B",
                "options": [{"id": "B", "source": "metabase"}],
            },
        }
    ]
    with (
        patch.object(agent, "_setup_context", new=AsyncMock(return_value="Break down orders")),
        patch.object(
            BaseSpecialistAgent, "run", new=AsyncMock(return_value=AgentResult(success=True, data="answer"))
        ) as run,
    ):
        result = await agent.run(
            "Break down orders", {"source_selection_history": history}, None, routing_adapter(), "test"
        )
    assert result.data == "answer"
    run.assert_awaited_once()
    assert "User-selected data sources for this turn: metabase" in agent.system_prompt


async def test_older_user_source_choice_is_available_for_semantic_legacy_resolution():
    from app.services.chat.source_selection import resolve_source_selection

    messages = [{"role": "user", "content": "Use Metabase"}]
    messages.extend({"role": "assistant", "content": "Done"} for _ in range(60))
    adapter = routing_adapter(sources=["metabase"], action="select")
    interpreted = await classify_request(task="Break down the orders", history=messages, adapter=adapter, model="test")
    import json

    payload = json.loads(adapter.create_message.call_args.kwargs["messages"][0]["content"])
    assert payload["legacy_user_requests"][0]["text"] == "Use Metabase"
    selection = resolve_source_selection(
        task="Break down the orders",
        tool_definitions=inventory(),
        conversation_history=messages,
        route=interpreted.route,
    )
    assert selection.question is None
    assert selection.selected_sources == ("metabase",)


@pytest.mark.parametrize("streaming", [True, False])
async def test_source_gate_only_classifies_before_returning_without_data_tools(streaming):
    agent = UnifiedAgent(
        tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), correlation_id="source-test", context_need="data"
    )
    agent._tool_defs = inventory()
    adapter = routing_adapter(continuation=False)
    with patch.object(agent, "_setup_context", new=AsyncMock(return_value="Count batch 395 orders")):
        if streaming:
            events = [event async for event in agent.run_streaming("Count batch 395 orders", {}, None, adapter, "test")]
            result = events[-1][1]
        else:
            result = await agent.run("Count batch 395 orders", {}, None, adapter, "test")
    assert result.success and "Which data source" in result.data
    assert result.tool_calls_log == []
    adapter.create_message.assert_awaited_once()
    assert [t["name"] for t in adapter.create_message.call_args.kwargs["tools"]] == ["route_request"]
    assert result.tokens_used.input_tokens == 17
    assert result.request_context["pending_source"] is True


def test_metabase_selection_preserves_its_tools_and_drops_other_connectors():
    metabase = connector()
    definitions = build_external_tool_definitions([metabase])
    all_tools = definitions + [{"name": "netsuite_suiteql"}, {"name": "bigquery_sql"}]
    assert filter_tools_for_chosen_source(all_tools, "metabase", [metabase]) == definitions
    assert filter_tools_for_chosen_source(all_tools, "netsuite", [metabase]) == [{"name": "netsuite_suiteql"}]


async def test_clarification_card_accepts_the_actual_custom_metabase_connector():
    result = await intercept_clarify_call(
        tool_input={
            "ambiguity_summary": "Choose the source",
            "options": [
                {
                    "id": "A",
                    "title": "Metabase",
                    "rationale": "Solidus database",
                    "source": "metabase",
                    "is_default": True,
                },
                {"id": "B", "title": "NetSuite", "rationale": "ERP", "source": "netsuite", "is_default": False},
            ],
        },
        session_id="test",
        active_connectors=[source_provider_for_connector(connector()), "netsuite_mcp"],
        db=AsyncMock(),
    )
    assert isinstance(result, InterceptResult)
    assert result.structured_output["options"][0]["source"] == "metabase"
