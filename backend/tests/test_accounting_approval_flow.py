"""Exercise the actual agent card and approval orchestration without live writes."""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatMessage
from app.services.chat.agents.base_agent import BaseSpecialistAgent
from app.services.chat.agents.unified_agent import UnifiedAgent
from app.services.chat.llm_adapter import ToolUseBlock
from app.services.chat.orchestrator import run_chat_turn
from app.services.chat.write_confirmation_service import build_confirmation_payload
from app.services.chat.write_validator import ValidationResult
from tests.test_mutation_intercept import _llm_response, _stream_replay
from tests.test_tax_correction import proposal
from tests.test_write_confirm_orchestrator import _TENANT_ID, _USER_ID, _make_db, _make_session


async def test_group_handoff_emits_one_real_card_without_another_model_hop():
    from app.services.chat.write_confirmation_service import WriteConfirmationPayload
    from tests.test_accounting_group import group_fixture

    so, session = group_fixture()
    card = WriteConfirmationPayload(
        **{**so, "record_type": "invoice corrections", "proposed_fields": {"eligible_orders": 2}}
    )
    db = AsyncMock(spec=AsyncSession)
    db.info = {}
    agent = UnifiedAgent(tenant_id=_TENANT_ID, user_id=_USER_ID, correlation_id=str(uuid.uuid4()))
    adapter = MagicMock()
    hops = []

    async def stream(**kwargs):
        hops.append(kwargs)
        assert len(hops) == 1
        yield (
            "response",
            _llm_response(
                tool_blocks=[
                    ToolUseBlock(id="group", name="transaction_ops_accounting_group", input={"group_id": "a" * 32})
                ]
            ),
        )

    adapter.stream_message = stream
    adapter.build_assistant_message.return_value = {"role": "assistant", "content": []}
    adapter.build_tool_result_message.return_value = {"role": "user", "content": []}
    execute = AsyncMock(return_value=json.dumps({"success": True, "case_count": 2, "financial_writes": 0}))
    prepare = AsyncMock(return_value=(card, "Prepared two exact corrections for approval."))
    with (
        patch("app.services.policy_service.get_active_policy", AsyncMock(return_value=None)),
        patch("app.services.chat.tools.execute_tool_call", execute),
        patch("app.services.transaction_ops.accounting_group.prepare_group_confirmation", prepare),
    ):
        events = [
            e
            async for e in BaseSpecialistAgent.run_streaming(
                agent, task="Fix all orders in this group", context={}, db=db, adapter=adapter, model="test-model"
            )
        ]
    assert len(hops) == 1 and execute.await_count == 1
    cards = [v for k, v in events if k == "confirmation_required"]
    assert len(cards) == 1 and len(cards[0]["accounting_group"]["members"]) == 2
    prepare.assert_awaited_once()


def inputs(p):
    return (
        f"ext__{p['connector_id'].replace('-', '')}__ns_updateRecord",
        {"recordType": "invoice", "recordId": p["record_id"], "data": json.dumps(p["proposed_fields"])},
    )


async def test_agent_emits_exact_accounting_card_without_executing_or_duplicate_prefetch():
    p = proposal()
    p["tenant_id"] = str(_TENANT_ID)
    name, params = inputs(p)
    db = AsyncMock(spec=AsyncSession)
    db.info = {"accounting_correction_candidate": p}
    agent = UnifiedAgent(tenant_id=_TENANT_ID, user_id=_USER_ID, correlation_id=str(uuid.uuid4()))
    adapter = MagicMock()
    adapter.stream_message = _stream_replay(
        [
            _llm_response(tool_blocks=[ToolUseBlock(id="write1", name=name, input=params)]),
            _llm_response(text="The exact correction is awaiting your approval."),
        ]
    )
    adapter.build_assistant_message.return_value = {"role": "assistant", "content": []}
    adapter.build_tool_result_message.return_value = {"role": "user", "content": []}
    execute = AsyncMock()
    with (
        patch("app.services.policy_service.get_active_policy", AsyncMock(return_value=None)),
        patch(
            "app.services.chat.agents.base_agent.validate_mutation", AsyncMock(return_value=ValidationResult(ok=True))
        ),
        patch("app.services.chat.tools.execute_tool_call", execute),
        patch(
            "app.services.mcp_connector_service.get_mcp_connector",
            AsyncMock(return_value=MagicMock(provider="netsuite_mcp")),
        ),
    ):
        events = [
            e
            async for e in BaseSpecialistAgent.run_streaming(
                agent,
                task="Propose the evidenced invoice correction",
                context={},
                db=db,
                adapter=adapter,
                model="test-model",
            )
        ]
    cards = [v for k, v in events if k == "confirmation_required"]
    assert len(cards) == 1
    assert cards[0]["record_id"] == p["record_id"]
    assert cards[0]["accounting_review"] == p
    assert cards[0]["proposed_fields"] == p["proposed_fields"]
    assert not cards[0].get("invariant_errors")
    execute.assert_not_awaited()
    text = " ".join(v for k, v in events if k == "text")
    assert "7030.02" in text and "7046.00" in text and "Posting period" in text


@pytest.mark.parametrize("outcome", ["stale", "verified", "unverified", "rejected"])
async def test_approval_preflight_execution_verification_and_actor_audit(outcome):
    p = proposal()
    p["tenant_id"] = str(_TENANT_ID)
    name, params = inputs(p)
    session_id = uuid.uuid4()
    card = build_confirmation_payload(
        mutation_type="update",
        record_type="invoice",
        tool_name=name,
        tool_input=params,
        session_id=str(session_id),
        current_record=p["before"],
    )
    card.accounting_review = p
    message = ChatMessage(
        id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        session_id=session_id,
        role="assistant",
        content="",
        structured_output={**card.model_dump(), "status": "pending"},
        created_at=datetime.now(timezone.utc),
    )
    db = _make_db(message)
    db.info = {}
    session = _make_session(session_id=str(session_id))
    order = []

    async def preflight(*args):
        order.append("preflight")
        if outcome == "stale":
            raise ValueError("NetSuite amount changed")

    async def execute(**kwargs):
        order.append("write")
        assert kwargs["human_approved"] is True
        assert kwargs["tool_input"] == params
        return json.dumps(
            {"error": "Period locked"} if outcome == "rejected" else {"success": True, "id": p["record_id"]}
        )

    async def verify(*args):
        order.append("verify")
        return {"status": "verified" if outcome == "verified" else "needs_review", "cash_settlement": "not_verified"}

    audit = AsyncMock()

    @asynccontextmanager
    async def locked(_):
        order.append("lock")
        try:
            yield
        finally:
            order.append("unlock")

    with (
        patch("app.services.transaction_ops.accounting_group.accounting_write_slot", locked),
        patch("app.services.transaction_ops.tax_correction.validate_approved", preflight),
        patch("app.services.transaction_ops.tax_correction.verify_after", verify),
        patch("app.services.chat.orchestrator.execute_tool_call", execute),
        patch("app.services.chat.orchestrator.log_event", audit),
        patch("app.services.mcp_connector_service.get_mcp_connector", AsyncMock(return_value=None)),
    ):
        events = [
            e
            async for e in run_chat_turn(
                db=db,
                session=session,
                user_message="approve",
                user_id=_USER_ID,
                tenant_id=_TENANT_ID,
                write_confirm={"action": "approve", "confirmation_id": str(message.id)},
            )
        ]
    assert order[0] == "lock" and order[-1] == "unlock"
    order = order[1:-1]
    if outcome == "stale":
        assert order == ["preflight"]
        assert message.structured_output["status"] == "failed"
        assert audit.await_args.kwargs["payload"]["financial_writes"] == 0
    elif outcome == "rejected":
        assert order == ["preflight", "write"]
        assert message.structured_output["repair_exit_reason"] == "fresh_accounting_evidence_required"
    else:
        assert order == ["preflight", "write", "verify"]
        so = message.structured_output
        assert so["accounting_verification"]["status"] == ("verified" if outcome == "verified" else "needs_review")
        verification = next(
            c.kwargs
            for c in audit.await_args_list
            if c.kwargs.get("action") == "accounting_correction.verification.completed"
        )
        assert verification["payload"]["approved_by"] == str(_USER_ID)
        assert verification["payload"]["before"] == p["before"]
        content = " ".join(e["message"]["content"] for e in events if e.get("type") == "message")
        assert ("independently re-read and verified" in content) == (outcome == "verified")
        if outcome == "unverified":
            assert "executed successfully" not in content


@pytest.mark.parametrize("blocked", [None, "validation", "policy", "tenant", "unavailable_tool"])
async def test_fresh_evidence_generates_real_card_without_second_model_hop(blocked):
    p = proposal()
    p["tenant_id"] = str(_TENANT_ID if blocked != "tenant" else uuid.uuid4())
    name, params = inputs(p)
    db = AsyncMock(spec=AsyncSession)
    db.info = {}
    agent = UnifiedAgent(tenant_id=_TENANT_ID, user_id=_USER_ID, correlation_id=str(uuid.uuid4()))
    read_name = "transaction_ops_accounting_evidence"
    agent._tool_defs = [{"name": read_name}]
    if blocked != "unavailable_tool":
        agent._tool_defs.append({"name": name})
    adapter = MagicMock()
    hops = []

    async def stream(**kwargs):
        hops.append(kwargs)
        assert len(hops) == 1, "A verified proposal must not spend another model hop composing prose"
        yield (
            "response",
            _llm_response(tool_blocks=[ToolUseBlock(id="read1", name=read_name, input={"case_id": "case"})]),
        )

    adapter.stream_message = stream
    adapter.build_assistant_message.return_value = {"role": "assistant", "content": []}
    adapter.build_tool_result_message.return_value = {"role": "user", "content": []}

    async def read(**kwargs):
        assert kwargs["tool_name"] == read_name
        db.info["accounting_correction_candidate"] = p
        return json.dumps({"success": True, "case_id": "case"})

    execute = AsyncMock(side_effect=read)
    validation = ValidationResult(
        ok=blocked != "validation", invariant_errors=["period unavailable"] if blocked == "validation" else []
    )
    with (
        patch("app.services.policy_service.get_active_policy", AsyncMock(return_value=None)),
        patch("app.services.chat.write_validation.validate_mutation", AsyncMock(return_value=validation)),
        patch("app.services.chat.tools.execute_tool_call", execute),
        patch(
            "app.services.policy_service.evaluate_tool_call",
            side_effect=lambda _, tool, __: {"allowed": not (blocked == "policy" and tool == name)},
        ),
        patch(
            "app.services.mcp_connector_service.get_mcp_connector",
            AsyncMock(return_value=MagicMock(provider="netsuite_mcp")),
        ),
    ):
        events = [
            e
            async for e in BaseSpecialistAgent.run_streaming(
                agent,
                task="Prepare the actual NetSuite correction for approval",
                context={},
                db=db,
                adapter=adapter,
                model="test-model",
            )
        ]
    assert len(hops) == 1
    assert execute.await_count == 1
    cards = [v for k, v in events if k == "confirmation_required"]
    if blocked:
        assert not cards
        assert next(v for k, v in events if k == "response").success is False
    else:
        assert len(cards) == 1
        assert cards[0]["record_id"] == p["record_id"]
        assert cards[0]["tool_input"] == params
        assert cards[0]["accounting_review"] == p
        result = next(v for k, v in events if k == "response")
        assert result.tokens_used.input_tokens == 10
        assert len(result.tool_calls_log) == 2
        assert json.loads(result.tool_calls_log[-1]["result_summary"])["financial_writes"] == 0
