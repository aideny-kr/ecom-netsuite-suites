"""TRANSACTION_OPS_DISPATCH_ENABLED: one operator switch for every path that can SEND a
financial write.

Enforced in code at the kernel's send permit, the Beat collector, the chat approve
branch and the durable group drain. Reads, recovery, evidence and existing approvals
are untouched; only sending stops, with an audited reason at each point.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.audit import AuditEvent
from app.services.transaction_ops import accounting_dispatch as dispatch
from app.services.transaction_ops import accounting_group as group
from app.services.transaction_ops import action_scheduler
from app.workers.base_task import InstrumentedTask
from tests import test_transaction_ops_executor as execution_fixtures
from tests.test_accounting_dispatch import seeded_group, simulated_executor
from tests.test_transaction_ops_executor import execute, operation
from tests.test_write_confirm_orchestrator import (
    _TENANT_ID,
    _USER_ID,
    _ext,
    _make_db,
    _make_real_confirmation_msg,
    _make_session,
)

execution_case = execution_fixtures.execution_case


@pytest.fixture
def dispatch_disabled(monkeypatch):
    monkeypatch.setattr(settings, "TRANSACTION_OPS_DISPATCH_ENABLED", False)


async def _audits(db, tenant_id, action):
    return list(
        await db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == tenant_id, AuditEvent.action == action))
    )


async def test_kernel_refuses_the_send_permit_and_fails_the_operation_blocked(db, execution_case, dispatch_disabled):
    result = await execute(db, execution_case)

    row = await operation(db, execution_case)
    assert row.status == "failed"
    assert row.result_json.get("dispatch_reserved") is not True  # refused BEFORE the one-use permit
    assert row.result_json["termination_reason"] == "blocked"
    assert row.result_json["code"] == "dispatch_disabled"
    assert result["status"] == "failed"
    audits = await _audits(db, execution_case.actor.tenant_id, "transaction_ops.operation.blocked")
    assert len(audits) == 1
    assert audits[0].payload["code"] == "dispatch_disabled"
    assert audits[0].payload["financial_writes"] == 0
    assert audits[0].payload["operation_id"] == str(row.id)
    # A duplicate delivery reads the terminal row and spends nothing.
    assert (await execute(db, execution_case))["status"] == "failed"
    assert execution_case.case.dispatch.await_count == 1


async def test_collector_publishes_nothing_and_audits_once_per_sweep(
    db, execution_case, dispatch_disabled, monkeypatch
):
    publish = AsyncMock()
    monkeypatch.setattr(action_scheduler, "_dispatch", publish)
    now = datetime.now(timezone.utc)

    stats = await action_scheduler.collect_due_actions(db, now)

    publish.assert_not_awaited()
    assert stats["termination_reason"] == "blocked"
    assert stats["dispatch_disabled"] is True
    assert stats["executions"] == 0
    audits = await _audits(db, uuid.UUID(InstrumentedTask.SYSTEM_TENANT_ID), "transaction_ops.dispatch.disabled")
    assert len(audits) == 1
    assert audits[0].payload == {"setting": "TRANSACTION_OPS_DISPATCH_ENABLED", "financial_writes": 0}

    # Re-enabling resumes: the approved work was never consumed by the switch.
    monkeypatch.setattr(settings, "TRANSACTION_OPS_DISPATCH_ENABLED", True)
    stats = await action_scheduler.collect_due_actions(db, now)
    assert stats["executions"] == 1 and stats["termination_reason"] == "done"
    publish.assert_awaited_once()


async def test_chat_approval_is_refused_before_the_dispatcher_and_ends_terminal(dispatch_disabled):
    from app.services.chat.orchestrator import run_chat_turn

    session_id = uuid.uuid4()
    tool_name = _ext("ns_createRecord")
    tool_input = {"recordType": "customer", "data": '{"companyname": "test ai customer"}'}
    confirm_msg = _make_real_confirmation_msg(session_id, tool_name, tool_input)
    db = _make_db(confirm_msg)
    session = _make_session(session_id=str(session_id))
    execute_tool = AsyncMock()
    log_event = AsyncMock(return_value=None)

    async def approve():
        with (
            patch("app.services.chat.orchestrator.execute_tool_call", execute_tool),
            patch("app.services.chat.orchestrator.log_event", log_event),
        ):
            return [
                event
                async for event in run_chat_turn(
                    db=db,
                    session=session,
                    user_message="approve",
                    user_id=_USER_ID,
                    tenant_id=_TENANT_ID,
                    write_confirm={"action": "approve", "confirmation_id": str(confirm_msg.id)},
                )
            ]

    events = await approve()

    execute_tool.assert_not_awaited()
    so = confirm_msg.structured_output
    assert so["status"] == "failed"
    assert so["repair_exit_reason"] == "dispatch_disabled"
    assert "disabled" in so["error"].lower()
    assert [e for e in events if e.get("type") == "error" and e.get("code") == "dispatch_disabled"]
    actions = [c.kwargs["action"] for c in log_event.call_args_list]
    assert "write.dispatch_disabled" in actions
    blocked = next(c for c in log_event.call_args_list if c.kwargs["action"] == "write.dispatch_disabled")
    assert blocked.kwargs["payload"]["financial_writes"] == 0

    # Terminal: the card never re-enters the repair loop and cannot be approved again.
    events = await approve()
    execute_tool.assert_not_awaited()
    assert any("not in a pending state" in (e.get("error") or "").lower() for e in events)


async def test_group_drain_halts_at_the_next_child_and_resumes_when_re_enabled(monkeypatch):
    monkeypatch.setattr(group, "CONCURRENCY", 1)  # one child at a time, so the flip lands between them
    async with seeded_group(2) as (factory, tenant, parent, so):
        members = [m["confirmation_id"] for m in so["accounting_group"]["members"]]
        calls = []
        base = simulated_executor(calls)

        async def flip_after_first(db, t, p, auth, member):
            await base(db, t, p, auth, member)
            monkeypatch.setattr(settings, "TRANSACTION_OPS_DISPATCH_ENABLED", False)

        monkeypatch.setattr(dispatch, "invoke_child", flip_after_first)

        async with factory() as db:
            result = await dispatch.run_slice(db, tenant, parent)
        assert len(calls) == 1
        first = calls[0]
        second = next(m for m in members if m != first)
        assert result["status"] == "blocked"
        async with factory() as db:
            work = (await dispatch.message(db, tenant, parent)).structured_output["accounting_group_dispatch"]
            assert work["members"][first]["status"] == "verified"
            assert work["members"][second]["status"] == "queued"  # untouched, resumes later
            assert work["status"] == "queued"
            audits = list(
                await db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.tenant_id == tenant, AuditEvent.action == "accounting_group.dispatch.disabled"
                    )
                )
            )
            assert len(audits) == 1 and audits[0].payload["financial_writes"] == 0

        monkeypatch.setattr(settings, "TRANSACTION_OPS_DISPATCH_ENABLED", True)
        async with factory() as db:
            result = await dispatch.run_slice(db, tenant, parent)
        assert calls == [first, second]
        assert result["status"] != "blocked"
        async with factory() as db:
            work = (await dispatch.message(db, tenant, parent)).structured_output["accounting_group_dispatch"]
            assert work["members"][second]["status"] == "verified"
            assert work["status"] == "finished"
