"""Real-Postgres actor, conversation, connector and current-policy checks."""

import copy
from unittest.mock import patch

import pytest

from app.mcp.tools import result_pivot
from app.models.chat import ChatSession
from app.models.mcp_connector import McpConnector
from tests.conftest import create_test_user
from tests.test_metabase_pivot import CID, CONN, TOOL, config, payload


async def setup_context(db, tenant, actor):
    session = ChatSession(tenant_id=tenant.id, user_id=actor.id)
    db.add(session)
    db.add(
        McpConnector(
            id=CID,
            tenant_id=tenant.id,
            label="Metabase",
            provider=CONN.provider,
            server_url=CONN.server_url,
            auth_type=CONN.auth_type,
            metadata_json=copy.deepcopy(CONN.metadata_json),
            is_enabled=True,
            status="active",
        )
    )
    await db.flush()
    return {"db": db, "tenant_id": str(tenant.id), "actor_id": str(actor.id), "conversation_id": str(session.id)}


def entries():
    return {
        "r1": {"tool": TOOL, "payload": payload([["SKU-A", "complete", 3], ["SKU-B", "complete", 2]])},
        "r2": {"tool": TOOL, "payload": payload([[4]], grouped=False)},
    }


@pytest.mark.asyncio
async def test_authorized_actor_transforms_full_bound_source(db, tenant_a, admin_user):
    actor, _ = admin_user
    ctx = await setup_context(db, tenant_a, actor)
    source = entries()
    with patch.object(result_pivot, "get_full_payload_entry", side_effect=lambda session, rid: source.get(rid)):
        result = await result_pivot.execute(config().model_dump(), ctx)
    assert result.get("error") is None
    assert result["rows"] == [["SKU-A", 3], ["SKU-B", 2]]
    assert "separately: 4" in result["caveats"][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "denial",
    [
        "foreign_tenant",
        "foreign_actor",
        "other_session_owner",
        "inactive_actor",
        "service_actor",
        "inactive_tenant",
        "missing_permission",
    ],
)
async def test_unauthorized_context_cannot_read_cache(db, tenant_a, tenant_b, admin_user, denial):
    actor, _ = admin_user
    ctx = await setup_context(db, tenant_a, actor)
    if denial == "foreign_tenant":
        ctx["tenant_id"] = str(tenant_b.id)
    elif denial == "foreign_actor":
        other, _ = await create_test_user(db, tenant_b)
        ctx["actor_id"] = str(other.id)
    elif denial == "other_session_owner":
        other, _ = await create_test_user(db, tenant_a)
        ctx["actor_id"] = str(other.id)
    elif denial == "inactive_actor":
        actor.is_active = False
    elif denial == "service_actor":
        actor.actor_type = "system"
    elif denial == "inactive_tenant":
        tenant_a.is_active = False
    else:
        from sqlalchemy import delete

        from app.models.user import UserRole

        await db.execute(delete(UserRole).where(UserRole.user_id == actor.id))
    await db.flush()
    with patch.object(result_pivot, "get_full_payload_entry") as cache:
        result = await result_pivot.execute(config().model_dump(), ctx)
    assert "error" in result
    cache.assert_not_called()


@pytest.mark.asyncio
async def test_foreign_connector_cannot_be_resolved_from_own_conversation(db, tenant_a, tenant_b, admin_user):
    actor, _ = admin_user
    ctx = await setup_context(db, tenant_a, actor)
    connection = await db.get(McpConnector, CID)
    connection.tenant_id = tenant_b.id
    await db.flush()
    source = entries()
    with patch.object(result_pivot, "get_full_payload_entry", side_effect=lambda session, rid: source.get(rid)):
        result = await result_pivot.execute(config().model_dump(), ctx)
    assert "unavailable" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["source_removed", "pivot_removed", "field_blocked"])
async def test_current_policy_applies_to_previously_cached_data(db, tenant_a, admin_user, restriction):
    from app.models.policy_profile import PolicyProfile

    actor, _ = admin_user
    ctx = await setup_context(db, tenant_a, actor)
    policy = PolicyProfile(
        tenant_id=tenant_a.id,
        name="Current",
        version=1,
        is_active=True,
        tool_allowlist=["pivot_query_result", TOOL],
        blocked_fields=[],
    )
    if restriction == "source_removed":
        policy.tool_allowlist = ["pivot_query_result"]
    elif restriction == "pivot_removed":
        policy.tool_allowlist = [TOOL]
    else:
        policy.blocked_fields = ["state"]
    db.add(policy)
    await db.flush()
    source = entries()
    with patch.object(result_pivot, "get_full_payload_entry", side_effect=lambda session, rid: source.get(rid)):
        result = await result_pivot.execute(config().model_dump(), ctx)
    assert "policy" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
async def test_agent_query_control_pivot_uses_full_results_and_verified_rendering(db, tenant_a, admin_user, streaming):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from app.services.chat.agents.base_agent import BaseSpecialistAgent
    from app.services.chat.llm_adapter import LLMResponse, ToolUseBlock
    from app.services.chat.metabase_evidence import MetabaseEvidence
    from app.services.chat.metabase_results import nonstream_interceptor
    from tests.test_metabase_pivot import make_result
    from tests.test_metabase_skills import _agent, _connector

    actor, _ = admin_user
    ctx = await setup_context(db, tenant_a, actor)
    connector = _connector(names=["query"])
    connector.id = CID
    agent = _agent([connector])
    agent.tenant_id, agent.user_id = tenant_a.id, actor.id
    agent._metabase_evidence = MetabaseEvidence({TOOL})
    agent._tool_defs.append({"name": "pivot_query_result", "description": "Pivot", "input_schema": {"type": "object"}})
    source, query = make_result([[f"SKU-{i}", "complete", 1] for i in range(60)])
    control, control_query = make_result([[60]], grouped=False)
    cache, outputs = {}, []

    def write_cache(session, rid, full, **kwargs):
        cache[(session, rid)] = {"payload": full, "tool": kwargs["tool_name"], "params": kwargs["params"]}

    step = 0

    async def response(**kwargs):
        nonlocal step
        step += 1
        if step == 1:
            return LLMResponse(tool_use_blocks=[ToolUseBlock("source", TOOL, {"query": query})])
        if step == 2:
            assert outputs[-1]["result_id"] == "r1"
            return LLMResponse(tool_use_blocks=[ToolUseBlock("control", TOOL, {"query": control_query})])
        if step == 3:
            assert outputs[-1]["result_id"] == "r2"
            return LLMResponse(tool_use_blocks=[ToolUseBlock("pivot", "pivot_query_result", config().model_dump())])
        assert "table_reference" in outputs[-1]
        return LLMResponse(text_blocks=[outputs[-1]["table_reference"]])

    async def stream(**kwargs):
        yield "response", await response(**kwargs)

    async def dispatch(tool_name, tool_input, **kwargs):
        if tool_name == TOOL:
            return json.dumps(source if tool_input["query"]["stages"][-1].get("breakout") else control)
        assert tool_name == "pivot_query_result"
        return json.dumps(await result_pivot.execute(tool_input, ctx))

    def build_tool_result(content):
        outputs.extend(json.loads(part["content"]) for part in content if part.get("type") == "tool_result")
        return {"role": "user", "content": content}

    adapter = MagicMock()
    adapter.create_message = response
    adapter.stream_message = stream
    adapter.build_assistant_message.side_effect = lambda r: {"role": "assistant", "content": ""}
    adapter.build_tool_result_message.side_effect = build_tool_result
    with (
        patch("app.services.chat.result_cache.cache_full_payload", side_effect=write_cache),
        patch.object(
            result_pivot, "get_full_payload_entry", side_effect=lambda session, rid: cache.get((session, rid))
        ),
        patch("app.services.chat.mutation_guard.classify_connector_mutation", new=AsyncMock(return_value=None)),
        patch("app.services.chat.tools.execute_tool_call", side_effect=dispatch),
        patch(
            "app.services.chat.agents.base_agent.extract_structured_confidence",
            new=AsyncMock(return_value=SimpleNamespace(score=5, source="test")),
        ),
        patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", new=AsyncMock()),
    ):
        if streaming:
            interceptor = await nonstream_interceptor(db, tenant_a.id, ctx["conversation_id"], [])
            events = [
                event
                async for event in BaseSpecialistAgent.run_streaming(
                    agent,
                    "Pivot orders by SKU and state",
                    {},
                    db,
                    adapter,
                    "test",
                    session_id=ctx["conversation_id"],
                    tool_result_interceptor=interceptor,
                )
            ]
            rendered = [value[1] for kind, value in events if kind == "tool_intercept"]
            assert len(rendered) == 1 and len(rendered[0]["rows"]) == 60
            answer = events[-1][1]
        else:
            answer = await BaseSpecialistAgent.run(
                agent,
                "Pivot orders by SKU and state",
                {},
                db,
                adapter,
                "test",
                session_id=ctx["conversation_id"],
            )
    assert "SKU-59" in answer.data and "separately: 60" in answer.data
    assert "mb_ref" not in answer.data and len(answer.tool_calls_log) == 3
    assert [call["result_id"] for call in answer.tool_calls_log] == ["r1", "r2", "r3"]
