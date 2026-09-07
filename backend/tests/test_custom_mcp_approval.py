"""Unknown custom tools cannot bypass the existing signed approval flow."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.chat.mutation_guard import classify_connector_mutation
from app.services.chat.tools import _execute_external_tool
from app.services.chat.write_confirmation_service import build_confirmation_payload, validate_and_extract_confirmation


@pytest.mark.parametrize("raw_name", ["send_money", "read_orders", "ns_runCustomSuiteQL", "ns_createRecord"])
async def test_custom_tools_always_require_confirmation(raw_name, monkeypatch):
    identifier, tenant = uuid4(), uuid4()
    monkeypatch.setattr(
        "app.services.mcp_connector_service.get_mcp_connector",
        AsyncMock(return_value=SimpleNamespace(provider="custom", is_enabled=True)),
    )
    call = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("app.services.mcp_client_service.call_external_mcp_tool", call)
    assert await classify_connector_mutation(f"ext__{identifier.hex}__{raw_name}", AsyncMock(), tenant) == "execute"
    blocked = await _execute_external_tool(identifier, raw_name, {"amount": "100.00"}, tenant, AsyncMock())
    assert blocked["hitl_required"] is True
    call.assert_not_called()
    assert await _execute_external_tool(
        identifier, raw_name, {"amount": "100.00"}, tenant, AsyncMock(), human_approved=True
    ) == {"ok": True}
    call.assert_awaited_once()


def test_custom_confirmation_displays_and_signs_exact_arbitrary_input():
    tool = f"ext__{uuid4().hex}__send_money"
    data = {"id": "payee-1", "amount": "100.00", "options": {"currency": "EUR"}}
    card = build_confirmation_payload(
        mutation_type="execute",
        record_type="external tool send_money",
        tool_name=tool,
        tool_input=data,
        session_id="session-1",
    )
    assert card.proposed_fields == data
    assert card.tool_input == data
    assert card.unvalidated and not card.editable_slots
    stored = card.model_dump()
    valid, name, extracted = validate_and_extract_confirmation(stored, "session-1")
    assert valid and name == tool and extracted == data
    stored["tool_input"] = {**data, "amount": "10000.00"}
    assert not validate_and_extract_confirmation(stored, "session-1")[0]


@pytest.mark.parametrize("http_api", [False, True])
async def test_streaming_custom_tool_produces_a_card_without_execution(monkeypatch, http_api):
    from unittest.mock import MagicMock

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.chat.agents.base_agent import BaseSpecialistAgent
    from app.services.chat.agents.unified_agent import UnifiedAgent
    from app.services.chat.llm_adapter import ToolUseBlock
    from tests.test_mutation_intercept import _llm_response, _stream_replay

    tenant, identifier = uuid4(), uuid4()
    inputs = {"id": "payee-123", "amount": "100.00", "ask_user": "a real custom argument"}
    tool_name = f"ext__{identifier.hex}__transfer_money"
    if http_api:
        tool_name = f"http__{identifier.hex}__get"
        inputs = {"path": "orders?per_page=1"}
        monkeypatch.setattr(
            "app.services.chat.http_connector_tools.describe_target",
            AsyncMock(return_value="API GET · Warehouse"),
        )
    adapter = MagicMock()
    adapter.stream_message = _stream_replay(
        [
            _llm_response(tool_blocks=[ToolUseBlock(id="custom-1", name=tool_name, input=inputs)]),
            _llm_response(text="Waiting for your approval."),
        ]
    )
    adapter.build_assistant_message.return_value = {"role": "assistant", "content": []}
    adapter.build_tool_result_message.return_value = {"role": "user", "content": []}
    monkeypatch.setattr("app.services.policy_service.get_active_policy", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.services.mcp_connector_service.get_mcp_connector",
        AsyncMock(return_value=SimpleNamespace(provider="custom", is_enabled=True)),
    )
    execute = AsyncMock()
    monkeypatch.setattr("app.services.chat.tools.execute_tool_call", execute)
    agent = UnifiedAgent(tenant_id=tenant, user_id=uuid4(), correlation_id=str(uuid4()))
    events = [
        event
        async for event in BaseSpecialistAgent.run_streaming(
            agent,
            task="Run the requested tool",
            context={},
            db=AsyncMock(spec=AsyncSession),
            adapter=adapter,
            model="test-model",
        )
    ]
    cards = [body for kind, body in events if kind == "confirmation_required"]
    assert len(cards) == 1, events
    assert cards[0]["mutation_type"] == "execute"
    assert cards[0]["tool_input"] == inputs
    assert cards[0]["proposed_fields"] == inputs
    assert cards[0]["unvalidated"] is True
    execute.assert_not_called()
