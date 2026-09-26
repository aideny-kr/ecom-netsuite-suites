import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from app.services.chat import external_tool_audit as mod


@pytest.fixture
def audit(monkeypatch):
    events = AsyncMock()
    monkeypatch.setattr(mod, "append_event", events)
    return events


async def call(execute, **kwargs):
    return await mod.audited_external_call(
        execute=execute,
        tenant_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        actor_type="user",
        correlation_id="turn",
        session_id="chat",
        connector_id=uuid.uuid4(),
        tool_name="ext__query",
        params={"sqlQuery": "SELECT id FROM transaction", "data": [{"access_token": "secret"}]},
        human_approved=kwargs.get("approved", False),
    )


@pytest.mark.parametrize(
    "result,action",
    [
        ({"data": [1]}, "tool.executed"),
        ({"error": "invalid query"}, "tool.failed"),
        ({"isError": True}, "tool.failed"),
        ({"success": False}, "tool.failed"),
    ],
)
async def test_each_call_records_request_and_actual_outcome_with_actor_and_scope(audit, result, action):
    out = await call(AsyncMock(return_value=result))
    assert out == result
    first, last = [c.kwargs for c in audit.await_args_list]
    assert first["action"] == "tool.requested"
    assert last["action"] == action
    assert first["actor_id"] == last["actor_id"]
    assert first["tenant_id"] == last["tenant_id"]
    assert last["correlation_id"] == "turn"
    assert last["payload"]["session_id"] == "chat"
    assert last["payload"]["params"]["data"][0]["access_token"] == "[redacted]"
    assert len(last["payload"]["result_sha256"]) == 64
    assert last["payload"]["approved_by"] is None


async def test_audit_request_failure_prevents_external_execution(audit):
    audit.side_effect = RuntimeError("DB unavailable")
    execute = AsyncMock()
    with pytest.raises(RuntimeError):
        await call(execute)
    execute.assert_not_awaited()


async def test_completion_audit_failure_preserves_receipt_and_forbids_retry(audit):
    audit.side_effect = [None, RuntimeError("DB unavailable")]
    out = await call(AsyncMock(return_value={"recordId": "20"}), approved=True)
    assert out["recordId"] == "20"
    assert out["audit_completion_pending"] is True
    assert "Do not retry" in out["instruction"]
    assert audit.await_args_list[0].kwargs["payload"]["approved_by"]


async def test_cancelled_request_is_audited_as_unknown(audit):
    with pytest.raises(asyncio.CancelledError):
        await call(AsyncMock(side_effect=asyncio.CancelledError()))
    assert audit.await_args_list[-1].kwargs["status"] == "unknown"
    assert audit.await_args_list[-1].kwargs["action"] == "tool.interrupted"


def test_json_encoded_payload_redacts_credentials_but_retains_financial_values():
    import json

    result = mod.redact({"data": '{"password":"hidden","taxRate":4.9999404}'})
    assert json.loads(result["data"]) == {"password": "[redacted]", "taxRate": 4.9999404}


async def test_a_request_that_could_not_be_recorded_is_raised_as_never_sent(audit):
    # R619946522 (2026-09-26): the request row failed on a dead pooled connection, nothing was
    # sent, and the write kernel could only record "unknown", which locked the credit memo.
    audit.side_effect = RuntimeError("Event loop is closed")
    execute = AsyncMock()
    with pytest.raises(mod.ExternalCallNotSentError):
        await call(execute)
    execute.assert_not_awaited()


class _Session:
    def __init__(self, used):
        self.used = used

    async def __aenter__(self):
        self.used.append("caller")
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        pass


async def test_every_audit_row_uses_the_callers_session_factory_not_the_app_pool(monkeypatch):
    # A Celery task runs in its own event loop; the app-wide pool keeps connections from an
    # earlier task's loop and fails with "Event loop is closed" when it touches them.
    used = []

    def app_pool():
        raise AssertionError("the app-wide pool was used from a worker loop")

    monkeypatch.setattr(mod, "async_session_factory", app_pool)
    monkeypatch.setattr(mod, "set_tenant_context", AsyncMock())
    monkeypatch.setattr(mod, "log_event", AsyncMock())
    out = await mod.audited_external_call(
        execute=AsyncMock(return_value={"data": [1]}),
        tenant_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        actor_type="user",
        correlation_id="turn",
        session_id="chat",
        connector_id=uuid.uuid4(),
        tool_name="ext__update",
        params={},
        human_approved=True,
        session_factory=lambda: _Session(used),
    )
    assert out == {"data": [1]}
    assert used == ["caller", "caller"]  # the request row and the outcome row


def test_the_callers_factory_is_the_one_the_dispatcher_prepared():
    from types import SimpleNamespace

    factory = object()
    prepared = SimpleNamespace(info={"accounting_authorization_session_factory": factory})
    assert mod.session_factory_for(prepared) is factory
    assert mod.session_factory_for(SimpleNamespace(info={})) is None
    assert mod.session_factory_for(None) is None
    # A stand-in session (a test double) never supplies a factory by accident.
    from unittest.mock import MagicMock

    assert mod.session_factory_for(MagicMock()) is None
