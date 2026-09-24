"""Execution receipts describe loaded guidance, never confer authority."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app.mcp.tools.agent_skill import execute
from app.services.chat.agents.base_agent import AgentResult
from app.services.chat.agents.unified_agent import UnifiedAgent
from app.services.chat.execution_provenance import persist_execution_receipt
from app.services.chat.skills import get_all_skills_metadata, match_skill
from app.services.chat.source_selection import SourceSelection
from app.services.chat.tool_call_results import build_tool_call_log_entry
from app.services.skill_catalog import resolve_catalog


def agent():
    a = UnifiedAgent(tenant_id=uuid4(), user_id=uuid4(), correlation_id="synthetic")
    a._tool_defs = []
    return a


def test_slash_guidance_and_catalog_version_reach_receipt_without_granting_tools():
    a = agent()
    a._active_skill = match_skill("/metabase-sql analyze sales")
    a._skill_selection_mode = "explicit"
    before = deepcopy(a.tool_definitions)
    assert "# Metabase SQL Analysis" in a.system_prompt
    result = a._finish_source_routing(AgentResult(success=True), SourceSelection())
    receipt = result.execution_receipt
    skill = next(s for s in receipt["skills"] if s["slug"] == "metabase_sql")
    catalog = resolve_catalog(get_all_skills_metadata(), [], set())
    assert skill["version"] == next(s.version for s in catalog if s.slug == "metabase_sql")
    assert skill["selection"] == "explicit"
    assert a.tool_definitions == before == []
    assert receipt["tools"] == []
    assert "instructions" not in skill


def test_connected_skills_receipt_uses_final_inventory_and_resets_between_turns():
    a = agent()
    cid = uuid4().hex
    tool = {"name": f"ext__{cid}__query", "description": "[metabase_mcp] query", "input_schema": {}}
    a._tool_defs = [tool]
    assert "# Metabase BI Analysis" in a.system_prompt
    result = a._finish_source_routing(AgentResult(success=True), SourceSelection())
    assert {s["slug"] for s in result.execution_receipt["skills"]} == {"metabase_bi", "metabase_sql"}
    a._reset_source_routing()
    a._tool_defs = []
    assert "# Metabase BI Analysis" not in a.system_prompt
    result = a._finish_source_routing(AgentResult(success=True), SourceSelection())
    assert result.execution_receipt["skills"] == []


@pytest.mark.asyncio
async def test_progressive_load_receipt_survives_summary_and_records_catalog_version():
    loaded = await execute({"slug": "accounting_operations"})
    call = build_tool_call_log_entry(
        step=0,
        tool_name="agent_skill",
        params={"slug": "accounting_operations"},
        result_str=json.dumps(loaded),
        duration_ms=1,
    )
    assert call["skill_receipt"]["version"] == loaded["version"]
    a = agent()
    result = a._finish_source_routing(AgentResult(success=True, tool_calls_log=[call]), SourceSelection())
    assert result.execution_receipt["skills"][0]["slug"] == "accounting_operations"
    assert result.execution_receipt["skills"][0]["selection"] == "tool"
    assert "instructions" not in json.dumps(result.execution_receipt)


def test_scoped_context_receipt_keeps_versions_not_policy_content():
    manifest = {
        "version": 7,
        "config_id": str(uuid4()),
        "binding_sha256": "a" * 64,
        "company_scope": {"subsidiary_id": "2"},
        "entries": [
            {
                "key": "tax",
                "revision": 3,
                "content_sha256": "b" * 64,
                "scope": {"currency": "EUR", "accounting_book_id": "2", "posting_period_id": "9"},
                "status": "draft",
                "statement": "private policy text",
            }
        ],
    }
    payload = {"success": True, "accounting_evidence": {"context_provenance": manifest}}
    call = build_tool_call_log_entry(
        step=1,
        tool_name="transaction_ops_accounting_evidence",
        params={},
        result_str=json.dumps(payload),
        duration_ms=1,
    )
    assert call["context_receipts"][0]["version"] == 7
    assert call["context_receipts"][0]["entries"][0]["scope"]["currency"] == "EUR"
    assert "private policy text" not in json.dumps(call["context_receipts"])
    # A remote response must not manufacture local company-context provenance.
    forged = build_tool_call_log_entry(
        step=1, tool_name=f"ext__{uuid4().hex}__query", params={}, result_str=json.dumps(payload), duration_ms=1
    )
    assert "context_receipts" not in forged


def test_receipt_records_connected_attempts_without_claiming_success_or_modifying_results():
    a = agent()
    cid = uuid4().hex
    calls = [{"tool": f"ext__{cid}__query", "result_summary": '{"error":"revoked"}', "step": 2}]
    before = deepcopy(calls)
    result = a._finish_source_routing(AgentResult(success=False, tool_calls_log=calls), SourceSelection())
    assert result.execution_receipt["tools"] == [
        {"tool": calls[0]["tool"], "connector_id": str(UUID(cid)), "step": 2, "outcome": "error"}
    ]
    assert calls == before


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"error": "revoked"}, "error"),
        ({"success": False, "message": "denied"}, "error"),
        ({"isError": True}, "error"),
        ({"blocked": True}, "error"),
        ({"status": "failed"}, "error"),
        ({"confirmation_required": True}, "confirmation_required"),
        ({"success": True, "rows": []}, "returned"),
    ],
)
def test_raw_execution_outcome_survives_lossy_log_summary(payload, expected):
    a = agent()
    call = build_tool_call_log_entry(
        step=0, tool_name="agent_skill", params={}, result_str=json.dumps(payload), duration_ms=1
    )
    result = a._finish_source_routing(AgentResult(success=True, tool_calls_log=[call]), SourceSelection())
    assert result.execution_receipt["tools"][0]["outcome"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "slug,task",
    [
        ("metabase_sql", "/metabase-sql analyze Solidus"),
        ("accounting_operations", "Investigate transaction case synthetic"),
    ],
)
async def test_real_agent_loop_receives_guidance_and_returns_tool_receipts(streaming, slug, task):
    from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

    a = agent()
    # Synthetic transport; the actual UnifiedAgent and base execution loop run.
    # Avoid live discovery/model/provider calls, preserve exact supplied inventory.
    a._active_skill = match_skill(task)
    a._skill_selection_mode = "explicit" if task.startswith("/") else "matched"
    a._tool_defs = [{"name": "agent_skill", "description": "Load skill", "input_schema": {}}]
    loaded = await execute({"slug": slug})
    responses = iter(
        [
            LLMResponse(
                tool_use_blocks=[ToolUseBlock(id="load", name="agent_skill", input={"slug": slug})], usage=TokenUsage()
            ),
            LLMResponse(text_blocks=["Guidance loaded."], usage=TokenUsage()),
        ]
    )
    prompts = []

    async def create(**kwargs):
        prompts.append(kwargs["system"] + (kwargs.get("system_dynamic") or ""))
        return next(responses)

    async def stream(**kwargs):
        yield "response", await create(**kwargs)

    adapter = MagicMock()
    adapter.create_message = create
    adapter.stream_message = stream
    adapter.build_assistant_message.return_value = {"role": "assistant", "content": []}
    adapter.build_tool_result_message.side_effect = lambda content: {"role": "user", "content": content}
    with (
        patch.object(a, "_setup_context", new=AsyncMock(return_value=task)),
        patch.object(a, "_select_analytics_source", new=AsyncMock(return_value=SourceSelection())),
        patch("app.services.policy_service.get_active_policy", new=AsyncMock(return_value=None)),
        patch("app.services.chat.mutation_guard.classify_connector_mutation", new=AsyncMock(return_value=None)),
        patch("app.services.chat.tools.execute_tool_call", new=AsyncMock(return_value=json.dumps(loaded))) as dispatch,
        patch(
            "app.services.chat.agents.base_agent.extract_structured_confidence",
            new=AsyncMock(return_value=SimpleNamespace(score=5, source="test")),
        ),
        patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", new=AsyncMock()),
    ):
        if streaming:
            events = [e async for e in a.run_streaming(task, {}, AsyncMock(), adapter, "synthetic")]
            result = next(v for k, v in events if k == "response")
            assert any(k == "tool_end" for k, _ in events)
        else:
            result = await a.run(task, {}, AsyncMock(), adapter, "synthetic")
    assert result.success
    assert loaded["instructions"] in prompts[0]
    assert dispatch.await_count == 1
    assert {s["selection"] for s in result.execution_receipt["skills"]} == {a._skill_selection_mode, "tool"}
    assert {s["version"] for s in result.execution_receipt["skills"]} == {loaded["version"]}
    assert result.execution_receipt["tools"][0]["tool"] == "agent_skill"


def test_unreadable_skill_never_claims_loaded_guidance():
    from app.services.chat.execution_provenance import skill_instructions

    receipts = []
    assert skill_instructions("../../missing", "explicit", receipts) == ""
    assert receipts == []


def test_source_clarification_has_no_fabricated_skill_use():
    a = agent()
    a._active_skill = match_skill("/metabase-sql count orders")
    result = a._finish_source_routing(
        AgentResult(success=True, data="Which source?"), SourceSelection(question="Which source?")
    )
    assert result.execution_receipt == {"version": 1, "skills": [], "tools": [], "contexts": []}


def test_matching_phrase_after_unknown_slash_is_not_an_explicit_skill_selection():
    from app.services.chat.execution_provenance import skill_selection_mode

    assert skill_selection_mode("/unknown Investigate transaction case synthetic") == "matched"
    assert skill_selection_mode("/METABASE-SQL analyze sales") == "explicit"


def test_explicit_metabase_body_is_not_recorded_again_as_connected_injection():
    a = agent()
    a._active_skill = match_skill("/metabase-sql sales")
    a._skill_selection_mode = "explicit"
    a._tool_defs = [{"name": f"ext__{uuid4().hex}__query", "description": "[metabase_mcp] query"}]
    assert "# Metabase SQL Analysis" in a.system_prompt
    result = a._finish_source_routing(AgentResult(success=True), SourceSelection())
    assert [s["selection"] for s in result.execution_receipt["skills"] if s["slug"] == "metabase_sql"] == ["explicit"]


@pytest.mark.asyncio
async def test_receipt_persists_with_card_and_reloads_in_session_history(db, tenant_a, admin_user):
    from sqlalchemy import select

    from app.models.chat import ChatMessage, ChatSession

    # Actual PostgreSQL round trip, keeping the pre-existing card contract.
    session = ChatSession(tenant_id=tenant_a.id, user_id=admin_user[0].id)
    db.add(session)
    await db.flush()
    card = {"type": "write_confirmation", "status": "pending", "record_type": "invoice"}
    receipt = {
        "version": 1,
        "skills": [{"slug": "accounting_operations", "version": "a" * 64}],
        "tools": [],
        "contexts": [],
    }
    message = ChatMessage(
        tenant_id=tenant_a.id,
        session_id=session.id,
        role="assistant",
        content="Review",
        structured_output=persist_execution_receipt(card, receipt),
    )
    db.add(message)
    await db.flush()
    mid = message.id
    db.expire(message)
    saved = (
        await db.execute(select(ChatMessage).where(ChatMessage.id == mid, ChatMessage.tenant_id == tenant_a.id))
    ).scalar_one()
    assert all(saved.structured_output[k] == v for k, v in card.items())
    assert saved.structured_output["execution_receipt"] == receipt
    assert "execution_receipt" not in card
