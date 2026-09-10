"""Metabase skills must reach the final LLM prompt through real MCP tool wiring."""

from __future__ import annotations

import uuid
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chat.agents.base_agent import BaseSpecialistAgent
from app.services.chat.agents.unified_agent import UnifiedAgent, _build_connected_systems_block, _build_role_prompt
from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock
from app.services.chat.metabase_context import build_metabase_skill_context, is_metabase_connector
from app.services.chat.orchestrator import _assemble_system_prompt
from app.services.chat.skills import get_skill_instructions, match_skill
from app.services.chat.tools import build_all_tool_definitions, build_external_tool_definitions


def _connector(*, provider="custom", url="https://bi.example.com/api/metabase-mcp", metadata=None, names=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider=provider,
        label="Solidus analytics",
        server_url=url,
        metadata_json=metadata,
        discovered_tools=[
            {
                "name": name,
                "description": f"Remote description for {name}",
                "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}}},
            }
            for name in (
                names if names is not None else ["search", "read_resource", "construct_query", "execute_query"]
            )
        ],
    )


def _agent(connectors, *, task="Analyze Solidus sales"):
    agent = UnifiedAgent(tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), correlation_id="metabase-test")
    agent._connectors = connectors
    agent._tool_defs = build_external_tool_definitions(connectors)
    agent._current_task = task
    return agent


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({}, True),
        ({"url": "https://bi.example.com/api/metabase-mcp/"}, True),
        ({"provider": "metabase_mcp", "url": "https://bi.example.com/mcp"}, True),
        ({"provider": "metabase", "url": "https://bi.example.com/mcp"}, True),
        ({"metadata": {"oauth_provider": "metabase"}, "url": "https://bi.example.com/mcp"}, True),
        ({"url": "https://metabase.example.com/unrelated-mcp"}, False),
        ({"url": "https://bi.example.com/api/metabase-mcp-impostor"}, False),
        ({"url": "https://[invalid"}, False),
        ({"url": None}, False),
        ({"provider": "netsuite_mcp"}, False),
    ],
)
def test_connector_identity_is_explicit(kwargs, expected):
    assert is_metabase_connector(_connector(**kwargs)) is expected


@pytest.mark.parametrize("slug,command", [("metabase_bi", "/metabase-bi"), ("metabase_sql", "/metabase-sql")])
def test_slash_skills_are_registered(slug, command):
    assert match_skill(f"{command} Solidus last month")["slug"] == slug
    assert get_skill_instructions(slug)


def test_actual_tool_inventory_attaches_both_skills_to_unified_prompt():
    connector = _connector()
    agent = _agent([connector])
    prompt = agent.system_prompt
    assert prompt.count("# Metabase BI Analysis") == 1
    assert prompt.count("# Metabase SQL Analysis") == 1
    assert prompt.index("<metabase_analysis_context>") > prompt.index("STOP WHEN YOU HAVE DATA")
    assert "finish discovery and validation" in prompt.lower()
    for raw in ("search", "read_resource", "construct_query", "execute_query"):
        full_name = f"ext__{connector.id.hex}__{raw}"
        assert full_name in prompt
        assert next(t for t in agent.tool_definitions if t["name"] == full_name)["description"].startswith(
            "[metabase_mcp] Solidus analytics:"
        )
    # The read-scoped native server does not acquire SQL or write tools.
    assert {t["name"].split("__", 2)[2] for t in agent.tool_definitions} == {
        "search",
        "read_resource",
        "construct_query",
        "execute_query",
    }
    assert "Native SQL requires an available SQL execution tool" in prompt
    assert "unexecuted SQL" in prompt


@pytest.mark.parametrize("template", ["Custom prompt {{TOOL_INVENTORY}}", "Custom prompt without a placeholder"])
def test_legacy_and_custom_template_paths_receive_skills(template):
    definitions = build_external_tool_definitions([_connector()])
    prompt = _assemble_system_prompt(template=template, tool_definitions=definitions)
    assert "{{TOOL_INVENTORY}}" not in prompt
    assert "# Metabase BI Analysis" in prompt
    assert "# Metabase SQL Analysis" in prompt


def test_filtered_out_connector_cannot_inject_skills_from_stale_connector_metadata():
    agent = _agent([_connector()])
    agent._tool_defs = [{"name": "clarify", "description": "Ask for scope", "input_schema": {"type": "object"}}]
    prompt = agent.system_prompt
    assert "<metabase_analysis_context>" not in prompt
    assert "# Metabase SQL Analysis" not in prompt


def test_unrelated_generic_tools_and_remote_mentions_do_not_activate_skills():
    connector = _connector(url="https://other.example.com/mcp")
    for tool in connector.discovered_tools:
        tool["description"] = "[metabase_mcp] Search things like Metabase"
    definitions = build_external_tool_definitions([connector])
    assert build_metabase_skill_context(definitions) == ""
    assert "<metabase_analysis_context>" not in _agent([connector]).system_prompt
    assert build_metabase_skill_context([]) == ""


def test_local_tool_cannot_activate_external_source_guidance():
    assert build_metabase_skill_context([{"name": "execute_sql", "description": "[metabase_mcp] local"}]) == ""


@pytest.mark.parametrize("provider,expected_calls", [("custom", 3), ("netsuite_mcp", 2)])
async def test_metabase_validation_queries_are_not_skipped_or_told_to_stop(provider, expected_calls):
    import json

    connector = _connector(provider=provider, names=["search", "query"])
    agent = _agent([connector])
    agent._context_need = "data"
    query_name = f"ext__{connector.id.hex}__query"
    responses = iter(
        [
            LLMResponse(
                text_blocks=[],
                tool_use_blocks=[ToolUseBlock(id="metadata", name=f"ext__{connector.id.hex}__search", input={})],
                usage=TokenUsage(),
            ),
            LLMResponse(
                text_blocks=[],
                tool_use_blocks=[
                    ToolUseBlock(id="total", name=query_name, input={"scope": "total"}),
                    ToolUseBlock(id="status", name=query_name, input={"scope": "status"}),
                ],
                usage=TokenUsage(),
            ),
            LLMResponse(text_blocks=["Validated."], tool_use_blocks=[], usage=TokenUsage()),
        ]
    )
    seen_messages = []

    async def stream(**kwargs):
        seen_messages.append(deepcopy(kwargs["messages"]))
        yield "response", next(responses)

    adapter = MagicMock()
    adapter.stream_message = stream
    adapter.build_assistant_message.return_value = {"role": "assistant", "content": []}
    adapter.build_tool_result_message.side_effect = lambda content: {"role": "user", "content": content}
    dispatch = AsyncMock(
        side_effect=[
            json.dumps({"tables": ["orders"]}),
            json.dumps({"rows": [[65]]}),
            json.dumps({"rows": [["complete", 41], ["canceled", 24]]}),
        ]
    )
    with (
        patch("app.services.policy_service.get_active_policy", new=AsyncMock(return_value=None)),
        patch("app.services.chat.mutation_guard.classify_connector_mutation", new=AsyncMock(return_value=None)),
        patch("app.services.chat.tools.execute_tool_call", new=dispatch),
        patch(
            "app.services.chat.agents.base_agent.extract_structured_confidence",
            new=AsyncMock(return_value=SimpleNamespace(score=5, source="test")),
        ),
        patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", new=AsyncMock()),
    ):
        events = [
            event
            async for event in BaseSpecialistAgent.run_streaming(
                agent, "Count orders by status", {}, AsyncMock(), adapter, "test"
            )
        ]
    assert events[-1][1].success
    assert dispatch.await_count == expected_calls
    final_messages = json.dumps(seen_messages[-1])
    assert ("A query returned data successfully" in final_messages) is (provider != "custom")


def test_native_cloud_builder_tools_explain_the_actual_query_contract():
    connector = _connector(
        url="https://framework.metabaseapp.com/api/metabase-mcp",
        metadata={"oauth_provider": "metabase"},
        names=["query"],
    )
    connector.auth_type = "oauth2"
    original_schema = deepcopy(connector.discovered_tools[0]["input_schema"])
    tool = build_external_tool_definitions([connector])[0]
    assert "query must be MBQL 5" in tool["description"]
    assert "distinct order_id" in tool["description"]
    assert tool["input_schema"] == original_schema


def test_multiple_connections_keep_tool_groups_and_source_routing():
    first, second = _connector(), _connector(names=["search", "execute_sql"])
    second.label = "Other warehouse"
    netsuite = _connector(provider="netsuite_mcp", names=["ns_runCustomSuiteQL"])
    agent = _agent([first, second, netsuite])
    context = build_metabase_skill_context(agent.tool_definitions)
    for connector in [first, second]:
        line = next(line for line in context.splitlines() if line.startswith(f"- Connector {connector.id.hex}:"))
        assert all(f"ext__{connector.id.hex}__{t['name']}" in line for t in connector.discovered_tools)
    assert netsuite.id.hex not in context
    assert "Explicit requests for another source still take precedence" in context
    assert "Other warehouse" in agent.system_prompt
    assert "EXECUTION PRIORITY FOR NETSUITE" in agent.system_prompt


@pytest.mark.parametrize("slug", ["metabase_bi", "metabase_sql"])
def test_explicit_skill_is_not_duplicated_by_automatic_injection(slug):
    agent = _agent([_connector()])
    agent._active_skill = {"slug": slug}
    assert agent.system_prompt.count(get_skill_instructions(slug)) == 1


def test_role_and_connected_systems_describe_metabase_instead_of_generic_custom():
    connector = _connector()
    assert "Metabase" in _build_role_prompt([connector], "Test")
    assert "underlying database's dialect" in _build_connected_systems_block([connector])


async def test_tenant_gated_inventory_drives_skill_isolation():
    tenant_with_metabase, tenant_without_metabase = uuid.uuid4(), uuid.uuid4()
    connector = _connector()

    async def connectors_for_tenant(db, tenant_id):
        return [connector] if tenant_id == tenant_with_metabase else []

    with (
        patch("app.services.chat.tools.build_local_tool_definitions", return_value=[]),
        patch("app.services.chat.http_connector_tools.build_definitions", new=AsyncMock(return_value=[])),
        patch(
            "app.services.mcp_connector_service.get_active_connectors_for_tenant",
            new=AsyncMock(side_effect=connectors_for_tenant),
        ),
    ):
        with_tools = await build_all_tool_definitions(AsyncMock(), tenant_with_metabase)
        without_tools = await build_all_tool_definitions(AsyncMock(), tenant_without_metabase)
    assert "# Metabase BI Analysis" in _assemble_system_prompt(template="Test", tool_definitions=with_tools)
    assert build_metabase_skill_context(without_tools) == ""


def test_methodology_covers_solidus_metric_and_sql_failure_modes():
    bi = get_skill_instructions("metabase_bi")
    sql = get_skill_instructions("metabase_sql")
    for phrase in [
        "NOT a verified tenant schema",
        "completed_at",
        "do not subtract both",
        "currency",
        "cohort",
        "partial periods",
    ]:
        assert phrase in bi
    for phrase in [
        "read-only WITH ... SELECT",
        "data-modifying CTEs",
        "COUNT(DISTINCT order_id)",
        "SUM(DISTINCT amount)",
        "continuation tokens",
        "permission error is a boundary",
        "not an executed result",
        "same connector",
        "half-open time windows",
        '"conditions":',
        '"join-alias": "items"',
        '"aggregation": [["distinct"',
    ]:
        assert phrase in sql
