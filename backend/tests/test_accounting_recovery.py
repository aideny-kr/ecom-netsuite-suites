"""Real DB proof of durable approval attribution and bounded read-only recovery."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.chat import ChatMessage
from app.services.chat.orchestrator import _cas_claim_write_confirmation
from app.services.chat.write_confirmation_service import build_confirmation_payload
from app.services.transaction_ops import accounting_recovery as mod
from tests.test_accounting_approval_flow import inputs
from tests.test_accounting_recheck import approved_credit  # noqa: F401


@pytest.fixture(params=["credit", "discount", "tax"])
async def interrupted_credit(db, approved_credit, request):  # noqa: F811
    from tests.conftest import enable_feature_flag

    actor, config, case, message, source, target = approved_credit
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, flag)
    p = message.structured_output["accounting_review"]
    if request.param == "discount":
        p = {
            **p,
            "kind": "invoice_sales_adjustment",
            "record_type": "invoice",
            "mutation_type": "update",
            "proposed_fields": {"discountItem": {"id": "50"}, "discountRate": -5},
        }
    elif request.param == "tax":
        p = {**p, "record_type": "invoice", "mutation_type": "update", "proposed_fields": {"taxRate": "5"}}
        p.pop("kind")
        p.pop("profile")
    name, params = inputs(p)
    card = build_confirmation_payload(
        mutation_type="create" if request.param == "credit" else "update",
        record_type="creditmemo" if request.param == "credit" else "invoice",
        tool_name=name,
        tool_input=params,
        session_id=str(message.session_id),
        current_record=p["before"],
    )
    card.accounting_review = p
    now = datetime.now(timezone.utc) - timedelta(minutes=6)
    so = mod.execution_claim(card.model_dump(mode="json"), message.id, actor.id, {}, now=now)
    message.structured_output = so
    await db.flush()
    assert await _cas_claim_write_confirmation(db, message, so, "executing")
    await db.refresh(message)
    return actor, config, case, message, source, target


@pytest.fixture
def providers(monkeypatch):
    @asynccontextmanager
    async def lock(_):
        yield

    verify = AsyncMock(return_value={"status": "verified", "credit_memo_id": "30"})
    monkeypatch.setattr("app.services.transaction_ops.accounting_group.accounting_write_slot", lock)
    monkeypatch.setattr("app.services.transaction_ops.sales_credit.verify_after", verify)
    monkeypatch.setattr("app.services.transaction_ops.invoice_discount.verify_after", verify)
    monkeypatch.setattr("app.services.transaction_ops.tax_correction.verify_after", verify)
    return verify


async def test_recovery_preserves_original_approver_and_queues_once(db, interrupted_credit, providers):
    actor, _, case, message, _, _ = interrupted_credit
    now = datetime.now(timezone.utc)
    assert await mod.candidates(db, actor.tenant_id, now, limit=10) == [message.id]
    result = await mod.recover(db, actor.tenant_id, message.id, now=now)
    assert result == {"termination_reason": "done", "financial_writes": 0}
    assert message.structured_output["status"] == "approved"
    assert message.structured_output["accounting_recheck"]["status"] == "queued"
    assert case.status == "open"
    await mod.recover(db, actor.tenant_id, message.id, now=now + timedelta(minutes=10))
    providers.assert_awaited_once()
    assert await mod.candidates(db, actor.tenant_id, now, limit=10) == []
    events = list((await db.scalars(select(AuditEvent).where(AuditEvent.resource_id == str(message.id)))).all())
    claim = next(e for e in events if e.action == mod.CLAIM_ACTION)
    done = next(e for e in events if e.action == "accounting_recovery.completed")
    assert claim.actor_id == actor.id
    assert done.actor_type == "system" and done.actor_id is None
    assert done.payload["approved_by"] == str(actor.id)
    assert done.payload["financial_writes"] == 0


@pytest.mark.parametrize("problem", ["tenant", "digest", "actor", "payload", "inactive"])
async def test_recovery_refuses_untrusted_scope_before_native_reads(
    db, interrupted_credit, providers, tenant_b, problem
):
    actor, _, _, message, _, _ = interrupted_credit
    so = message.structured_output
    if problem == "digest":
        so = {**so, "accounting_execution": {**so["accounting_execution"], "evidence_digest": "changed"}}
    elif problem == "actor":
        so = {**so, "accounting_execution": {**so["accounting_execution"], "approved_by": str(tenant_b.id)}}
    elif problem == "payload":
        so = {**so, "tool_input": {**so["tool_input"], "data": "{}"}}
    elif problem == "inactive":
        actor.is_active = False
    message.structured_output = so
    await db.flush()
    await mod.recover(db, tenant_b.id if problem == "tenant" else actor.tenant_id, message.id)
    providers.assert_not_awaited()


async def test_unknown_outcome_stops_after_durable_budget_without_resetting_approval(db, interrupted_credit, providers):
    actor, _, _, message, _, _ = interrupted_credit
    providers.return_value = {"status": "needs_review", "reason": "credit_not_found", "retry_allowed": False}
    now = datetime.now(timezone.utc)
    for attempt in range(3):
        result = await mod.recover(db, actor.tenant_id, message.id, now=now + attempt * mod.DELAY)
        assert message.structured_output["status"] == "indeterminate"
        assert message.structured_output["accounting_execution"]["attempts"] == attempt + 1
    assert result["termination_reason"] == "budget"
    await mod.recover(db, actor.tenant_id, message.id, now=now + 4 * mod.DELAY)
    assert providers.await_count == 3
    assert await mod.candidates(db, actor.tenant_id, now + 4 * mod.DELAY, limit=10) == []


async def test_recovery_commit_precedes_provider_read_and_process_interruption_consumes_attempt(
    db, interrupted_credit, providers
):
    actor, _, _, message, _, _ = interrupted_credit

    class ProcessStopped(BaseException):
        pass

    async def interrupted(*args):
        event = await db.scalar(
            select(AuditEvent).where(
                AuditEvent.resource_id == str(message.id), AuditEvent.action == "accounting_recovery.started"
            )
        )
        assert event.payload["attempts"] == 1
        assert message.structured_output["accounting_execution"]["attempts"] == 1
        raise ProcessStopped()

    providers.side_effect = interrupted
    now = datetime.now(timezone.utc)
    with pytest.raises(ProcessStopped):
        await mod.recover(db, actor.tenant_id, message.id, now=now)
    await db.rollback()
    await db.refresh(message)
    await db.refresh(actor)
    assert message.structured_output["accounting_execution"]["attempts"] == 1
    assert not mod.eligible(message.structured_output, now)
    providers.side_effect = None
    await mod.recover(db, actor.tenant_id, message.id, now=now + mod.DELAY)
    assert message.structured_output["status"] == "approved"


async def test_invoice_lock_contention_never_reads_or_consumes_attempt(db, interrupted_credit, providers, monkeypatch):
    actor, _, _, message, _, _ = interrupted_credit

    @asynccontextmanager
    async def busy(_):
        raise ValueError("busy")
        yield

    monkeypatch.setattr("app.services.transaction_ops.accounting_group.accounting_write_slot", busy)
    result = await mod.recover(db, actor.tenant_id, message.id)
    assert result["termination_reason"] == "busy"
    await db.refresh(message)
    assert message.structured_output["accounting_execution"]["attempts"] == 0
    providers.assert_not_awaited()


async def test_scheduler_dispatches_credit_recovery_and_preserves_receipt(
    db, interrupted_credit, providers, monkeypatch
):
    from app.services.transaction_ops import action_scheduler

    actor, _, _, message, _, _ = interrupted_credit
    so = message.structured_output
    message.structured_output = {**so, "accounting_execution": {**so["accounting_execution"], "receipt": {"id": "999"}}}
    await db.flush()
    dispatch = AsyncMock()
    monkeypatch.setattr(action_scheduler, "_dispatch", dispatch)
    stats = await action_scheduler.collect_due_actions(db, datetime.now(timezone.utc))
    assert stats["credit_recoveries"] == 1
    dispatch.assert_awaited_once()
    assert dispatch.call_args.args[:3] == (actor.tenant_id, "credit_recover", message.id)
    await mod.recover(db, actor.tenant_id, message.id)
    assert message.structured_output["accounting_execution"]["receipt"] == {"id": "999"}
    if message.structured_output["accounting_review"].get("kind") in {
        "sales_adjustment_credit",
        "invoice_sales_adjustment",
    }:
        assert providers.call_args.args[3] == {"id": "999"}
    else:
        assert providers.call_args.args[2]["record_id"] == message.structured_output["accounting_review"]["record_id"]


async def test_group_refresh_uses_durable_children_without_starting_unsubmitted_members(
    db, interrupted_credit, providers
):
    actor, _, _, message, _, _ = interrupted_credit
    other = ChatMessage(
        tenant_id=actor.tenant_id,
        session_id=message.session_id,
        role="assistant",
        content="not submitted",
        structured_output={"status": "pending"},
    )
    db.add(other)
    await db.flush()
    parent = ChatMessage(
        tenant_id=actor.tenant_id,
        session_id=message.session_id,
        role="assistant",
        content="interrupted",
        structured_output={
            "status": "executing",
            "accounting_group": {
                "members": [
                    {"confirmation_id": str(message.id), "card": {"status": "executing"}},
                    {"confirmation_id": str(other.id), "card": {"status": "pending"}},
                ]
            },
        },
    )
    db.add(parent)
    await db.flush()
    await mod.recover(db, actor.tenant_id, message.id)
    await mod.refresh_group(db, actor.tenant_id, message.session_id, parent.id)
    assert parent.structured_output["status"] == "indeterminate"
    assert parent.structured_output["accounting_group"]["members"][0]["card"]["status"] == "approved"
    assert parent.structured_output["accounting_group"]["members"][1]["card"]["status"] == "pending"
    assert other.structured_output["status"] == "pending"
    providers.assert_awaited_once()
