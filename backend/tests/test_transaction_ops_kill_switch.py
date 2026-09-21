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
from sqlalchemy import select, text

from app.core.config import settings
from app.models.audit import AuditEvent
from app.models.transaction_ops import TransactionOperation
from app.services.transaction_ops import accounting_dispatch as dispatch
from app.services.transaction_ops import accounting_group as group
from app.services.transaction_ops import action_scheduler
from app.services.transaction_ops.state_service import StateError
from app.workers.base_task import InstrumentedTask
from tests import test_transaction_ops_dispatch as dispatch_fixtures
from tests import test_transaction_ops_executor as execution_fixtures
from tests import test_transaction_ops_recovery as recovery_fixtures
from tests.test_accounting_dispatch import seeded_group, simulated_executor
from tests.test_transaction_ops_dispatch import reserve
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
unknown_case = recovery_fixtures.unknown_case
ready = dispatch_fixtures.ready


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
    assert row.status == "rejected_before_effect"
    assert row.result_json.get("dispatch_reserved") is not True  # refused BEFORE the one-use permit
    assert row.result_json["termination_reason"] == "blocked"
    assert row.result_json["code"] == "dispatch_disabled"
    assert result["status"] == "rejected_before_effect"
    audits = await _audits(db, execution_case.actor.tenant_id, "transaction_ops.operation.blocked")
    assert len(audits) == 1
    assert audits[0].payload["code"] == "dispatch_disabled"
    assert audits[0].payload["financial_writes"] == 0
    assert audits[0].payload["operation_id"] == str(row.id)
    # A duplicate delivery reads the terminal row and spends nothing.
    assert (await execute(db, execution_case))["status"] == "rejected_before_effect"
    assert execution_case.case.dispatch.await_count == 1


async def test_kernel_reports_budget_exhaustion_over_the_switch_when_both_apply(db, ready, dispatch_disabled):
    """The switch is the LAST refusal before the permit. An operation that would have
    failed anyway keeps its more specific terminal reason, so an operator reading the
    failed rows after re-enabling dispatch can tell which ones a retry could not save."""
    actor, _, _, claim = ready
    await db.execute(
        text("UPDATE transaction_ops_operations SET api_calls_used = max_api_calls WHERE id = :operation"),
        {"operation": claim.operation_id},
    )
    await db.flush()

    with pytest.raises(StateError) as refused:
        await reserve(db, actor.tenant_id, claim)

    assert refused.value.code == "operation_budget_exhausted"
    row = await db.scalar(
        select(TransactionOperation)
        .where(TransactionOperation.id == claim.operation_id)
        .execution_options(populate_existing=True)
    )
    assert row.status == "rejected_before_effect"
    assert row.result_json["termination_reason"] == "budget"
    assert row.result_json["code"] == "operation_budget_exhausted"
    assert row.result_json.get("dispatch_reserved") is not True
    assert await _audits(db, actor.tenant_id, "transaction_ops.operation.blocked") == []


async def test_collector_withholds_sends_audits_once_and_keeps_approved_work(
    db, execution_case, dispatch_disabled, monkeypatch
):
    publish = AsyncMock()
    monkeypatch.setattr(action_scheduler, "_dispatch", publish)
    now = datetime.now(timezone.utc)

    stats = await action_scheduler.collect_due_actions(db, now)

    publish.assert_not_awaited()
    assert stats["dispatch_disabled"] is True
    assert stats["withheld"] == 1 and stats["executions"] == 0
    assert stats["termination_reason"] == "blocked"
    audits = await _audits(db, uuid.UUID(InstrumentedTask.SYSTEM_TENANT_ID), "transaction_ops.dispatch.disabled")
    assert len(audits) == 1
    assert audits[0].payload == {"setting": "TRANSACTION_OPS_DISPATCH_ENABLED", "withheld": 1, "financial_writes": 0}

    # Re-enabling resumes: the approved work was never consumed by the switch.
    monkeypatch.setattr(settings, "TRANSACTION_OPS_DISPATCH_ENABLED", True)
    stats = await action_scheduler.collect_due_actions(db, now)
    assert stats["executions"] == 1 and stats["termination_reason"] == "done"
    publish.assert_awaited_once()


async def test_collector_still_publishes_read_only_recovery_while_dispatch_is_disabled(
    db, unknown_case, dispatch_disabled, monkeypatch
):
    row = await operation(db, unknown_case)
    assert row.status == "unknown"
    publish = AsyncMock()
    monkeypatch.setattr(action_scheduler, "_dispatch", publish)

    stats = await action_scheduler.collect_due_actions(db, datetime.now(timezone.utc))

    assert stats["recoveries"] == 1 and stats["executions"] == 0 and stats["withheld"] == 0
    publish.assert_awaited_once()
    assert publish.call_args.args[:3] == (unknown_case.actor.tenant_id, "recover", row.id)
    assert stats["dispatch_disabled"] is True and stats["termination_reason"] == "done"
    # Nothing was withheld, so nothing needs an audited reason.
    assert await _audits(db, uuid.UUID(InstrumentedTask.SYSTEM_TENANT_ID), "transaction_ops.dispatch.disabled") == []


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


async def test_group_rejection_drains_while_dispatch_is_disabled(monkeypatch, dispatch_disabled):
    """A reject sends nothing, so the switch must not freeze an operator's cancellation."""
    async with seeded_group(2, action="reject") as (factory, tenant, parent, so):
        calls = []
        monkeypatch.setattr(dispatch, "invoke_child", simulated_executor(calls))
        async with factory() as db:
            result = await dispatch.run_slice(db, tenant, parent)
        assert result["status"] == "finished"
        assert sorted(calls) == sorted(m["confirmation_id"] for m in so["accounting_group"]["members"])
        async with factory() as db:
            work = (await dispatch.message(db, tenant, parent)).structured_output["accounting_group_dispatch"]
            assert {m["status"] for m in work["members"].values()} == {"rejected"}
            audits = list(
                await db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.tenant_id == tenant, AuditEvent.action == "accounting_group.dispatch.disabled"
                    )
                )
            )
            assert audits == []


async def test_a_halted_accounting_card_is_a_zero_write_precondition_failure(dispatch_disabled):
    """An accounting correction halted by the switch must be released for a fresh approval once
    dispatch is back. previous_execution releases a failed card only when it carries the
    accounting_correction.precondition_failed audit with zero writes, so the approve branch
    must write that audit for accounting cards, not only write.dispatch_disabled. An MCP card
    runs through the write kernel, and the switch trips before the ledger claim: the halted
    card carries no execution record at all, so there is nothing a later card could mistake
    for a prior attempt."""
    from datetime import datetime, timezone

    from app.services.chat.orchestrator import run_chat_turn
    from tests.test_accounting_approval_flow import kind_proposal

    session_id = uuid.uuid4()
    tool_name = _ext("ns_updateRecord")
    p = kind_proposal("api_credit")
    p["tenant_id"] = str(_TENANT_ID)
    tool_input = {"recordType": "creditMemo", "recordId": p["record_id"], "data": p.get("wire_record_json", "{}")}
    confirm_msg = _make_real_confirmation_msg(session_id, tool_name, tool_input)
    confirm_msg.structured_output = {**confirm_msg.structured_output, "mutation_type": "update", "accounting_review": p}
    db = _make_db(confirm_msg)
    # The accounting approve path re-enters run_chat_turn under the per-invoice write slot
    # (a real advisory lock on the app engine); mark the slot as already held so the test
    # exercises the branch under it, not the lock.
    db.info = {"accounting_write_lock": str(confirm_msg.id)}
    session = _make_session(session_id=str(session_id))
    execute_tool = AsyncMock()
    log_event = AsyncMock(return_value=None)
    with (
        patch("app.services.chat.orchestrator.execute_tool_call", execute_tool),
        patch("app.services.chat.orchestrator.log_event", log_event),
        patch("app.services.transaction_ops.accounting_group.authorize_accounting_write", AsyncMock()),
        patch("app.services.transaction_ops.resolution_plan.previous_execution", AsyncMock(return_value=None)),
    ):
        events = [
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

    execute_tool.assert_not_awaited()
    assert [e for e in events if e.get("type") == "error" and e.get("code") == "dispatch_disabled"]
    so = confirm_msg.structured_output
    assert so["status"] == "failed" and so["repair_exit_reason"] == "dispatch_disabled"
    assert "accounting_execution" not in so and "operation_id" not in so
    # The CAS claim audit is logged positionally; the branch audits by keyword.
    by_action = {
        (c.kwargs.get("action") or (c.args[3] if len(c.args) > 3 else None)): (c.kwargs or {"positional": c.args})
        for c in log_event.call_args_list
    }
    assert "write.dispatch_disabled" in by_action
    released = by_action.get("accounting_correction.precondition_failed")
    assert released is not None, sorted(by_action)
    assert released["payload"]["approved_by"] == str(_USER_ID)
    assert released["payload"]["financial_writes"] == 0
    assert released["payload"]["reason"] == "dispatch_disabled"
    assert released["resource_id"] == str(confirm_msg.id)
    assert datetime.now(timezone.utc)  # keep the import honest


@pytest.mark.parametrize("released_by_precondition_audit", [False, True])
async def test_previous_execution_releases_a_halted_card_only_through_the_precondition_audit(
    db, admin_user, released_by_precondition_audit
):
    """The contract the test above depends on: without the zero-write precondition audit, a
    halted card blocks every later card for the same correction as a prior execution."""
    from datetime import datetime, timezone

    from app.models.chat import ChatMessage, ChatSession
    from app.services.audit_service import log_event
    from app.services.transaction_ops.accounting_recovery import execution_claim
    from app.services.transaction_ops.resolution_plan import previous_execution
    from tests.test_accounting_approval_flow import kind_proposal

    actor = admin_user[0]
    p = kind_proposal("api_credit")
    p["tenant_id"] = str(actor.tenant_id)
    session = ChatSession(tenant_id=actor.tenant_id, user_id=actor.id)
    db.add(session)
    await db.flush()
    halted_id = uuid.uuid4()
    so = execution_claim(
        {"accounting_review": p, "mutation_type": "update", "tool_name": "mcp", "tool_input": {}},
        halted_id,
        actor.id,
        {},
        now=datetime.now(timezone.utc),
    )
    so.update(
        status="failed",
        error="Sending to connected systems is disabled by the operator.",
        repair_exit_reason="dispatch_disabled",
    )
    db.add(
        ChatMessage(
            id=halted_id,
            tenant_id=actor.tenant_id,
            session_id=session.id,
            role="assistant",
            content="",
            structured_output=so,
        )
    )
    await db.flush()
    await log_event(
        db=db,
        tenant_id=actor.tenant_id,
        actor_id=actor.id,
        category="write",
        action="write.dispatch_disabled",
        resource_type="chat_message",
        resource_id=str(halted_id),
        payload={"setting": "TRANSACTION_OPS_DISPATCH_ENABLED", "financial_writes": 0},
        status="error",
    )
    if released_by_precondition_audit:
        await log_event(
            db=db,
            tenant_id=actor.tenant_id,
            actor_id=actor.id,
            category="transaction_ops",
            action="accounting_correction.precondition_failed",
            resource_type="chat_message",
            resource_id=str(halted_id),
            payload={"approved_by": str(actor.id), "financial_writes": 0, "reason": "dispatch_disabled"},
            status="error",
        )
    await db.flush()
    prior = await previous_execution(db, actor.tenant_id, uuid.uuid4(), p)
    assert (prior is None) is released_by_precondition_audit, prior


def test_group_member_halted_by_the_switch_is_blocked_not_verification_pending():
    """A member halted by the orchestrator's switch after its claim carries accounting_execution
    but nothing was sent: there is nothing to verify, and the member needs a fresh approval."""
    halted = {
        "status": "failed",
        "repair_exit_reason": "dispatch_disabled",
        "accounting_execution": {"approved_by": str(_USER_ID)},
    }
    assert dispatch.outcome(halted, "approve")["status"] == "blocked"
    # A genuine in-flight attempt still waits for verification and is never resent.
    executing = {"status": "executing", "accounting_execution": {"approved_by": str(_USER_ID)}}
    assert dispatch.outcome(executing, "approve")["status"] == "verification_pending"
