"""Run actual agent/orchestrator paths: no generic fallback during case repair."""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chat.agents.base_agent import BaseSpecialistAgent
from app.services.chat.agents.unified_agent import UnifiedAgent
from app.services.chat.llm_adapter import ToolUseBlock
from app.services.chat.orchestrator import run_chat_turn
from tests.test_mutation_intercept import _ext, _llm_response, _stream_replay
from tests.test_write_confirm_orchestrator import (
    _TENANT_ID,
    _USER_ID,
    _make_db,
    _make_real_confirmation_msg,
    _make_session,
)


@pytest.mark.parametrize("already_selected", [True, False])
@pytest.mark.parametrize(
    "tool,params",
    [
        ("ns_createRecord", {"recordType": "journalentry", "data": '{"memo":"correction"}'}),
        ("ns_updateRecord", {"recordType": "invoice", "id": "100", "data": '{"total":100}'}),
        ("ns_upsertRecord", {"recordType": "creditmemo", "data": '{"total":100}'}),
        ("ns_deleteRecord", {"recordType": "customerrefund", "id": "100"}),
        ("upsert_flow", {"id": "flow", "disabled": False}),
    ],
)
async def test_case_workflow_never_emits_generic_write_card(already_selected, tool, params):
    db = AsyncMock(spec=AsyncSession)
    db.info = {}
    agent = UnifiedAgent(tenant_id=_TENANT_ID, user_id=_USER_ID, correlation_id=str(uuid4()))
    agent._transaction_workflow = already_selected
    responses = []
    if not already_selected:
        responses.append(
            _llm_response(
                tool_blocks=[ToolUseBlock(id="case", name="transaction_ops_status", input={"case_id": str(uuid4())})]
            )
        )
    responses.extend(
        [
            _llm_response(tool_blocks=[ToolUseBlock(id="write", name=_ext(tool), input=params)]),
            _llm_response(text="The application evidence is incomplete. I cannot prepare that treatment yet."),
        ]
    )
    adapter = MagicMock()
    adapter.stream_message = _stream_replay(responses)
    adapter.build_assistant_message.return_value = {"role": "assistant", "content": []}
    adapter.build_tool_result_message.return_value = {"role": "user", "content": []}
    execute = AsyncMock(return_value=json.dumps({"success": True, "status": "open"}))
    validate = AsyncMock(side_effect=AssertionError("Generic validation must not imply accounting eligibility"))
    with (
        patch("app.services.policy_service.get_active_policy", AsyncMock(return_value=None)),
        patch("app.services.mcp_connector_service.get_mcp_connector", AsyncMock(return_value=None)),
        patch("app.services.chat.tools.execute_tool_call", execute),
        patch("app.services.chat.agents.base_agent.validate_mutation", validate),
    ):
        events = [
            e
            async for e in BaseSpecialistAgent.run_streaming(
                agent, task="Resolve this transaction case", context={}, db=db, adapter=adapter, model="test"
            )
        ]
    assert not [e for e in events if e[0] == "confirmation_required"]
    validate.assert_not_awaited()
    assert execute.await_count == (0 if already_selected else 1)
    result = [value for name, value in events if name == "response"][-1]
    assert "accounting_adapter_required" in json.dumps(result.tool_calls_log)
    assert not getattr(agent, "_write_proposal_forced", False)


async def test_old_transaction_generic_card_is_rejected_and_audited_before_execution():
    session_id = uuid4()
    message = _make_real_confirmation_msg(
        session_id, _ext("ns_createRecord"), {"recordType": "salesOrder", "data": '{"entity":{"id":"10"}}'}
    )
    message.structured_output = {
        **message.structured_output,
        "request_context": {"version": 1, "kind": "transaction", "sources": [], "pending_source": False},
    }
    db = _make_db(message)
    db.info = {}
    execute, audit = AsyncMock(), AsyncMock()
    with (
        patch("app.services.chat.orchestrator.execute_tool_call", execute),
        patch("app.services.chat.orchestrator.log_event", audit),
    ):
        events = [
            event
            async for event in run_chat_turn(
                db=db,
                session=_make_session(session_id=str(session_id)),
                user_message="approve",
                user_id=_USER_ID,
                tenant_id=_TENANT_ID,
                write_confirm={"confirmation_id": str(message.id), "action": "approve"},
            )
        ]
    execute.assert_not_awaited()
    assert message.structured_output["status"] == "failed"
    assert message.structured_output["repair_exit_reason"] == "fresh_accounting_evidence_required"
    assert any(event.get("type") == "error" and "generic card" in event.get("error", "") for event in events)
    assert audit.await_args.kwargs["action"] == "accounting_correction.unsupported_confirmation"
    assert audit.await_args.kwargs["payload"]["financial_writes"] == 0


async def test_accounting_metadata_research_does_not_force_an_extra_write_hop():
    db = AsyncMock(spec=AsyncSession)
    db.info = {}
    agent = UnifiedAgent(tenant_id=_TENANT_ID, user_id=_USER_ID, correlation_id=str(uuid4()))
    agent._transaction_workflow = True
    adapter = MagicMock()
    calls = []
    responses = [
        _llm_response(
            tool_blocks=[
                ToolUseBlock(id="metadata", name=_ext("ns_getRecordTypeMetadata"), input={"recordType": "creditmemo"})
            ]
        ),
        _llm_response(text="Metadata is available; the intended credit application still needs evidence."),
    ]

    async def stream(**kwargs):
        calls.append(kwargs)
        assert len(calls) <= 2, "Read-only accounting research must not compel a generic write"
        yield "response", responses[len(calls) - 1]

    adapter.stream_message = stream
    adapter.build_assistant_message.return_value = {"role": "assistant", "content": []}
    adapter.build_tool_result_message.return_value = {"role": "user", "content": []}
    with (
        patch("app.services.policy_service.get_active_policy", AsyncMock(return_value=None)),
        patch("app.services.mcp_connector_service.get_mcp_connector", AsyncMock(return_value=None)),
        patch("app.services.chat.tools.execute_tool_call", AsyncMock(return_value='{"success":true}')),
    ):
        events = [
            e
            async for e in BaseSpecialistAgent.run_streaming(
                agent, task="Investigate missing application evidence", context={}, db=db, adapter=adapter, model="test"
            )
        ]
    assert len(calls) == 2
    assert not any(name == "confirmation_required" for name, _ in events)
    assert not getattr(agent, "_prose_instead_of_write_bounced", False)
