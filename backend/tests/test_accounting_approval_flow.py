"""Exercise the actual agent card and approval orchestration without live writes."""

import json
import uuid
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
    with (
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
