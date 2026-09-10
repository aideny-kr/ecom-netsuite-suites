import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.chat.agents.unified_agent import UnifiedAgent
from app.services.chat.plan_mode.clarify_intercept import InterceptResult, intercept_clarify_call
from app.services.chat.plan_mode.short_circuit import filter_tools_for_chosen_source
from app.services.chat.plan_mode.source_resolver import source_provider_for_connector
from app.services.chat.source_selection import source_selection_question
from app.services.chat.tools import build_external_tool_definitions


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


@pytest.mark.parametrize(
    "question",
    [
        "How many orders in Sakura Batch 5 Laptops (batch id = 395) include FRANVW0016?",
        "How much revenue did we generate?",
        "What is the status of order R123?",
        "Do not use NetSuite",
        "Which source should we use, NetSuite or Metabase?",
    ],
)
def test_data_without_a_choice_asks_before_querying(question):
    result = source_selection_question(task=question, tool_definitions=inventory(), context_need="data")
    assert result and all(name in result for name in ("Metabase", "NetSuite", "BigQuery"))


@pytest.mark.parametrize(
    "question",
    [
        "Use Metabase",
        "Solidus",
        "In NetSuite, count orders",
        "Compare Metabase and NetSuite",
        "Use Metabase, not NetSuite",
    ],
)
def test_explicit_choice_or_comparison_does_not_ask_again(question):
    assert source_selection_question(task=question, tool_definitions=inventory()) is None


def test_only_user_history_can_establish_choice():
    kwargs = {"task": "Break that down by status", "tool_definitions": inventory()}
    assert source_selection_question(
        **kwargs, conversation_history=[{"role": "assistant", "content": "I used Metabase"}]
    )
    assert (
        source_selection_question(**kwargs, conversation_history=[{"role": "user", "content": "Use Metabase"}]) is None
    )
    assert (
        source_selection_question(
            **kwargs, conversation_history=[{"role": "user", "content": [{"type": "text", "text": "Use Metabase"}]}]
        )
        is None
    )


def test_no_query_gate_for_single_source_or_documentation():
    assert source_selection_question(task="Count orders", tool_definitions=inventory()[:1]) is None
    assert (
        source_selection_question(task="Explain SQL joins", tool_definitions=inventory(), context_need="docs") is None
    )


@pytest.mark.parametrize("streaming", [True, False])
async def test_source_gate_never_calls_model_or_data_tools(streaming):
    agent = UnifiedAgent(
        tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), correlation_id="source-test", context_need="data"
    )
    agent._tool_defs = inventory()
    adapter = AsyncMock()
    with patch.object(agent, "_setup_context", new=AsyncMock(return_value="Count batch 395 orders")):
        if streaming:
            events = [event async for event in agent.run_streaming("Count batch 395 orders", {}, None, adapter, "test")]
            result = events[-1][1]
        else:
            result = await agent.run("Count batch 395 orders", {}, None, adapter, "test")
    assert result.success and "Which data source" in result.data
    assert result.tool_calls_log == []
    assert adapter.mock_calls == []


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
