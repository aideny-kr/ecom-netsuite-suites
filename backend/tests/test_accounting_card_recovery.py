"""G3.2c: a card the write kernel claimed is recovered from the ledger, by reads only.

The process that sent the write may die between the permit and the readback. The ledger
row says what is known (executing with a consumed permit; or a receipt); after the row's
deadline the recovery scan finds it, settles it, reads the provider once under its own
budget, and only a proof moves it to verified. The card is rendered from the row.
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.chat import ChatMessage
from app.models.transaction_ops import TransactionRun
from app.services.chat.write_confirmation_service import build_confirmation_payload
from app.services.transaction_ops import accounting_recovery as mod
from app.services.transaction_ops import chat_confirmation
from app.services.transaction_ops import state_service as state
from tests.test_accounting_approval_flow import inputs
from tests.test_accounting_recheck import approved_credit  # noqa: F401
from tests.test_chat_confirmation_source import _row, authorized  # noqa: F401


@pytest.fixture
async def interrupted(db, approved_credit, authorized):  # noqa: F811
    """A credit card claimed by the kernel whose sender died after the permit: the ledger
    row is executing with its permit consumed, the card is executing."""
    actor, config, case, verified_message, _, _ = approved_credit
    p = verified_message.structured_output["accounting_review"]
    name, params = inputs(p)
    card = build_confirmation_payload(
        mutation_type="create",
        record_type=p["record_type"],
        tool_name=name,
        tool_input=params,
        session_id=str(verified_message.session_id),
        current_record=p["before"],
    )
    card.accounting_review = p
    message = ChatMessage(
        tenant_id=actor.tenant_id,
        session_id=verified_message.session_id,
        role="assistant",
        content="",
        structured_output={**card.model_dump(mode="json"), "status": "executing"},
    )
    db.add(message)
    await db.flush()
    claimed = await chat_confirmation.claim(db, actor.tenant_id, message, actor_id=actor.id)
    message.structured_output = {**message.structured_output, "operation_id": str(claimed.operation_id)}
    await db.flush()
    assert await state.reserve_operation_dispatch(
        db,
        actor.tenant_id,
        claimed,
        provider=chat_confirmation.PROVIDER_MCP,
        payload_fingerprint="a" * 64,
        authorize=chat_confirmation.authorize_dispatch,
    )
    return actor.tenant_id, actor.id, message.id, claimed  # ids only: a rollback expires the rows


@pytest.fixture
def readback(monkeypatch):
    verify = AsyncMock(return_value={"status": "verified", "credit_memo_id": "31"})
    monkeypatch.setattr("app.services.transaction_ops.sales_credit.verify_after", verify)
    return verify


async def test_an_interrupted_send_is_settled_after_its_deadline_and_verified_by_one_read(db, interrupted, readback):
    tenant_id, actor_id, message_id, claimed = interrupted
    now = datetime.now(timezone.utc)
    assert await mod.candidates(db, tenant_id, now, limit=10) == []  # its sender may still be alive
    with patch("app.services.transaction_ops.accounting_group.accounting_write_slot", _no_lock):
        assert (await mod.recover(db, tenant_id, message_id, now=now))["termination_reason"] == "busy"
        later = (await _row(db, claimed)).deadline_at + timedelta(seconds=1)
        assert await mod.candidates(db, tenant_id, later, limit=10) == [message_id]
        result = await mod.recover(db, tenant_id, message_id, now=later)
        assert result == {"termination_reason": "done", "financial_writes": 0}
        again = await mod.recover(db, tenant_id, message_id, now=later + timedelta(minutes=10))
        assert again["termination_reason"] == "done"
    readback.assert_awaited_once()
    message = await db.get(ChatMessage, message_id)
    await db.refresh(message)
    assert readback.await_args.args[2] == message.structured_output["accounting_review"]
    assert readback.await_args.args[3] == {}  # the kill came before any receipt
    row = await _row(db, claimed)
    assert row.status == "verified" and row.result_json["verification"]["credit_memo_id"] == "31"
    assert row.result_json["reconciled"] is True
    so = message.structured_output
    assert so["status"] == "approved" and so["accounting_verification"]["recovered_by_read"] is True
    assert so["accounting_recheck"]["status"] == "queued"
    assert "verified by read-only recovery" in message.content
    assert await mod.candidates(db, tenant_id, later + timedelta(minutes=10), limit=10) == []
    done = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.resource_id == str(message_id), AuditEvent.action == "accounting_recovery.completed"
        )
    )
    assert done.actor_type == "system" and done.actor_id is None
    assert done.payload["approved_by"] == str(actor_id) and done.payload["financial_writes"] == 0
    run = await db.scalar(
        select(TransactionRun).where(TransactionRun.params_json["operation_id"].astext == str(row.id))
    )
    assert run.origin == "recovery" and run.status == "finished"


async def test_a_readback_without_proof_leaves_the_row_unknown_and_the_card_in_review(db, interrupted, readback):
    tenant_id, actor_id, message_id, claimed = interrupted
    readback.return_value = {"status": "needs_review", "reason": "credit_not_found", "retry_allowed": False}
    later = (await _row(db, claimed)).deadline_at + timedelta(seconds=1)
    with patch("app.services.transaction_ops.accounting_group.accounting_write_slot", _no_lock):
        result = await mod.recover(db, tenant_id, message_id, now=later)
    assert result["termination_reason"] == "stall"
    row = await _row(db, claimed)
    assert row.status == "unknown" and "verification" not in row.result_json
    message = await db.get(ChatMessage, message_id)
    await db.refresh(message)
    so = message.structured_output
    assert so["status"] == "indeterminate" and so["accounting_verification"]["recovered_by_read"] is False
    assert "Do not repeat this write" in message.content
    assert "accounting_recheck" not in so
    # one automatic pass: the finished run keeps the scan from reading again
    assert await mod.candidates(db, tenant_id, later + timedelta(minutes=10), limit=10) == []


async def test_a_readback_failure_consumes_the_pass_and_reads_nothing_twice(db, interrupted, readback):
    tenant_id, actor_id, message_id, claimed = interrupted
    readback.side_effect = RuntimeError("provider unavailable")
    later = (await _row(db, claimed)).deadline_at + timedelta(seconds=1)
    with patch("app.services.transaction_ops.accounting_group.accounting_write_slot", _no_lock):
        result = await mod.recover(db, tenant_id, message_id, now=later)
        assert result["termination_reason"] == "error"
        assert (await mod.recover(db, tenant_id, message_id, now=later))["termination_reason"] == "done"
    assert readback.await_count == 1
    assert (await _row(db, claimed)).status == "unknown"
    message = await db.get(ChatMessage, message_id)
    await db.refresh(message)
    assert message.structured_output["accounting_verification"]["reason"] == "RuntimeError"


async def test_lock_contention_reads_nothing_and_consumes_no_pass(db, interrupted, readback):
    from contextlib import asynccontextmanager

    tenant_id, actor_id, message_id, claimed = interrupted

    @asynccontextmanager
    async def busy(*args, **kwargs):
        raise ValueError("busy")
        yield

    later = (await _row(db, claimed)).deadline_at + timedelta(seconds=1)
    with patch("app.services.transaction_ops.accounting_group.accounting_write_slot", busy):
        assert (await mod.recover(db, tenant_id, message_id, now=later))["termination_reason"] == "busy"
    readback.assert_not_awaited()
    assert await mod.candidates(db, tenant_id, later, limit=10) == [message_id]


@asynccontextmanager
async def _no_lock(*args, **kwargs):
    yield


@pytest.fixture
async def orphan(db, approved_credit):  # noqa: F811
    """A card whose process died after the card's own claim (status executing) and before
    the ledger's: no operation_id, no legacy accounting_execution, no ledger row."""
    actor, _, _, verified_message, _, _ = approved_credit
    p = verified_message.structured_output["accounting_review"]
    name, params = inputs(p)
    card = build_confirmation_payload(
        mutation_type="create",
        record_type=p["record_type"],
        tool_name=name,
        tool_input=params,
        session_id=str(verified_message.session_id),
        current_record=p["before"],
    )
    card.accounting_review = p
    message = ChatMessage(
        tenant_id=actor.tenant_id,
        session_id=verified_message.session_id,
        role="assistant",
        content="",
        structured_output={**card.model_dump(mode="json"), "status": "executing"},
    )
    db.add(message)
    await db.flush()
    return actor.tenant_id, actor.id, message.id


async def test_a_card_that_died_before_its_ledger_claim_is_released_after_the_grace_period(db, orphan):
    """No ledger row means no permit was ever minted, so nothing was sent: after the grace
    period the card is released (failed, with the reason) and the intent is free again."""
    tenant_id, _, message_id = orphan
    now = datetime.now(timezone.utc)
    assert await mod.candidates(db, tenant_id, now, limit=10) == []  # the sender may still be between commits
    later = now + mod.ORPHAN_GRACE + timedelta(seconds=1)
    assert await mod.candidates(db, tenant_id, later, limit=10) == [message_id]
    result = await mod.recover(db, tenant_id, message_id, now=later)
    assert result == {"termination_reason": "done", "financial_writes": 0}
    message = await db.get(ChatMessage, message_id)
    await db.refresh(message)
    assert message.structured_output["status"] == "failed"
    assert "nothing was sent" in message.structured_output["error"]
    released = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.resource_id == str(message_id),
            AuditEvent.action == "accounting_correction.precondition_failed",
        )
    )
    assert released.payload["financial_writes"] == 0 and released.actor_type == "system"
    assert await mod.candidates(db, tenant_id, later, limit=10) == []


async def test_an_orphan_release_re_checks_the_card_under_a_lock(db, orphan, monkeypatch):
    """Between the scan and the release, the sender may have caught up (a ledger row now
    exists for the card): the release re-reads the locked card and does nothing."""
    from app.services.transaction_ops import chat_confirmation

    tenant_id, actor_id, message_id = orphan
    later = datetime.now(timezone.utc) + mod.ORPHAN_GRACE + timedelta(seconds=1)
    original = mod._locked_message
    monkeypatch.setattr(chat_confirmation, "authorize_accounting_write", AsyncMock())

    async def caught_up(db_, tenant_id_, message_id_):
        locked = await original(db_, tenant_id_, message_id_)
        await chat_confirmation.claim(db_, tenant_id_, locked, actor_id=actor_id)  # the sender's claim lands
        return await original(db_, tenant_id_, message_id_)

    monkeypatch.setattr(mod, "_locked_message", caught_up)
    result = await mod.recover(db, tenant_id, message_id, now=later)
    assert result["termination_reason"] == "busy"
    message = await db.get(ChatMessage, message_id)
    await db.refresh(message)
    assert message.structured_output["status"] == "executing"
    assert await state.operation_for_approval(db, tenant_id, "chat_confirmation", message_id) is not None


async def test_the_scheduler_reaches_a_tenant_without_the_reconciliation_flags(db, interrupted, monkeypatch):
    """A card's tenant is found by its ledger rows, not by the scheduled feature's flags."""
    from unittest.mock import AsyncMock

    from app.services.transaction_ops import action_scheduler

    tenant_id, _, message_id, claimed = interrupted
    from app.services import feature_flag_service

    monkeypatch.setattr(feature_flag_service, "list_tenants_with_flags", AsyncMock(return_value=[]))
    publish = AsyncMock()
    monkeypatch.setattr(action_scheduler, "_dispatch", publish)
    later = (await _row(db, claimed)).deadline_at + timedelta(seconds=1)
    stats = await action_scheduler.collect_due_actions(db, later)
    assert stats["credit_recoveries"] == 1
    assert publish.call_args.args[:3] == (tenant_id, "credit_recover", message_id)


@pytest.fixture
async def config_less(db, approved_credit, authorized):  # noqa: F811
    """A card whose accounting review names its case but not its config (the invoice-tax
    builder never sets one), claimed by the kernel and interrupted after the permit."""
    actor, config, case, verified_message, _, _ = approved_credit
    p = dict(verified_message.structured_output["accounting_review"])
    p.pop("config_id", None)
    name, params = inputs(p)
    card = build_confirmation_payload(
        mutation_type="create",
        record_type=p["record_type"],
        tool_name=name,
        tool_input=params,
        session_id=str(verified_message.session_id),
        current_record=p["before"],
    )
    card.accounting_review = p
    message = ChatMessage(
        tenant_id=actor.tenant_id,
        session_id=verified_message.session_id,
        role="assistant",
        content="",
        structured_output={**card.model_dump(mode="json"), "status": "executing"},
    )
    db.add(message)
    await db.flush()
    claimed = await chat_confirmation.claim(db, actor.tenant_id, message, actor_id=actor.id)
    message.structured_output = {**message.structured_output, "operation_id": str(claimed.operation_id)}
    await db.flush()
    assert await state.reserve_operation_dispatch(
        db,
        actor.tenant_id,
        claimed,
        provider=chat_confirmation.PROVIDER_MCP,
        payload_fingerprint="a" * 64,
        authorize=chat_confirmation.authorize_dispatch,
    )
    return actor.tenant_id, message.id, claimed, config, case


async def test_a_card_without_a_config_takes_its_recovery_scope_from_its_case(db, config_less, readback):
    tenant_id, message_id, claimed, config, case = config_less
    row = await _row(db, claimed)
    assert row.result_json["recovery_scope"] == {"config_id": str(config.id), "order_reference": case.order_reference}
    later = row.deadline_at + timedelta(seconds=1)
    with patch("app.services.transaction_ops.accounting_group.accounting_write_slot", _no_lock):
        assert (await mod.recover(db, tenant_id, message_id, now=later))["termination_reason"] == "done"
    assert (await _row(db, claimed)).status == "verified"
    message = await db.get(ChatMessage, message_id)
    await db.refresh(message)
    assert message.structured_output["accounting_recheck"]["status"] == "queued"  # the recheck runs under that config


async def test_a_settled_row_with_no_scope_to_read_under_is_handed_to_a_person(db, interrupted, readback, monkeypatch):
    """No config means no budgeted read: the row is escalated (needs_review, code
    recovery_unscoped) instead of raising forever and blocking its document."""
    tenant_id, actor_id, message_id, claimed = interrupted
    row = await _row(db, claimed)
    row.result_json = {**row.result_json, "recovery_scope": {"config_id": None, "order_reference": None}}
    await db.flush()
    later = row.deadline_at + timedelta(seconds=1)
    with patch("app.services.transaction_ops.accounting_group.accounting_write_slot", _no_lock):
        result = await mod.recover(db, tenant_id, message_id, now=later)
    assert result["termination_reason"] == "blocked"
    readback.assert_not_awaited()
    row = await _row(db, claimed)
    assert row.status == "needs_review" and row.result_json["code"] == "recovery_unscoped"
    message = await db.get(ChatMessage, message_id)
    await db.refresh(message)
    assert message.structured_output["status"] == "indeterminate"
    assert message.structured_output["accounting_verification"]["reason"] == "recovery_unscoped"
    assert await mod.candidates(db, tenant_id, later, limit=10) == []


async def test_a_recovered_group_child_refreshes_its_parent(db, interrupted, readback):
    tenant_id, actor_id, message_id, claimed = interrupted
    child = await db.get(ChatMessage, message_id)
    parent = ChatMessage(
        tenant_id=tenant_id,
        session_id=child.session_id,
        role="assistant",
        content="interrupted",
        structured_output={
            "status": "executing",
            "accounting_group": {"members": [{"confirmation_id": str(child.id), "card": {"status": "executing"}}]},
        },
    )
    db.add(parent)
    await db.flush()
    row = await _row(db, claimed)
    context = {"confirmation_id": str(child.id), "group_approval_id": str(parent.id), "manifest_digest": "m" * 64}
    child.structured_output = {
        **child.structured_output,
        "accounting_group_child": True,
        "accounting_execution": chat_confirmation.execution_projection(child.id, row, context),
    }
    await db.flush()
    later = row.deadline_at + timedelta(seconds=1)
    with patch("app.services.transaction_ops.accounting_group.accounting_write_slot", _no_lock):
        assert (await mod.recover(db, tenant_id, message_id, now=later))["termination_reason"] == "done"
    await db.refresh(child)
    assert child.structured_output["accounting_execution"]["approval_context"] == context  # the link survives
    await db.refresh(parent)
    assert parent.structured_output["status"] == "approved"
    assert parent.structured_output["accounting_group"]["members"][0]["card"]["status"] == "approved"


async def test_the_scheduler_reaches_a_tenant_whose_only_stuck_work_is_an_orphan(db, orphan, monkeypatch):
    from app.services import feature_flag_service
    from app.services.transaction_ops import action_scheduler

    tenant_id, _, message_id = orphan
    monkeypatch.setattr(feature_flag_service, "list_tenants_with_flags", AsyncMock(return_value=[]))
    publish = AsyncMock()
    monkeypatch.setattr(action_scheduler, "_dispatch", publish)
    later = datetime.now(timezone.utc) + mod.ORPHAN_GRACE + timedelta(seconds=1)
    stats = await action_scheduler.collect_due_actions(db, later)
    assert stats["credit_recoveries"] == 1
    assert publish.call_args.args[:3] == (tenant_id, "credit_recover", message_id)


async def test_a_row_that_settled_while_its_card_never_heard_is_found_by_the_scan(db, interrupted, readback):
    """The sender may die between completing the row and writing the card: the row is
    terminal, the card still says executing. The scan must find that card and render it."""
    tenant_id, actor_id, message_id, claimed = interrupted
    await state.complete_operation(
        db, tenant_id, claimed.operation_id, outcome="needs_review", result_json={"code": "adapter_defect"}
    )
    now = datetime.now(timezone.utc)
    assert await mod.candidates(db, tenant_id, now, limit=10) == [message_id]
    with patch("app.services.transaction_ops.accounting_group.accounting_write_slot", _no_lock):
        assert (await mod.recover(db, tenant_id, message_id, now=now))["termination_reason"] == "done"
    message = await db.get(ChatMessage, message_id)
    await db.refresh(message)
    assert message.structured_output["status"] == "indeterminate"
    assert message.structured_output["accounting_execution"]["ledger_status"] == "needs_review"
    assert await mod.candidates(db, tenant_id, now, limit=10) == []
    readback.assert_not_awaited()


async def test_a_card_edited_since_its_claim_is_not_read_back_but_handed_to_a_person(db, interrupted, readback):
    tenant_id, actor_id, message_id, claimed = interrupted
    message = await db.get(ChatMessage, message_id)
    message.structured_output = {
        **message.structured_output,
        "tool_input": {**message.structured_output["tool_input"], "data": "{}"},
    }
    await db.flush()
    later = (await _row(db, claimed)).deadline_at + timedelta(seconds=1)
    with patch("app.services.transaction_ops.accounting_group.accounting_write_slot", _no_lock):
        result = await mod.recover(db, tenant_id, message_id, now=later)
    assert result["termination_reason"] == "blocked"
    readback.assert_not_awaited()
    row = await _row(db, claimed)
    assert row.status == "needs_review" and row.result_json["code"] == "confirmation_changed"


@pytest.fixture
async def claimed_no_permit(db, approved_credit, authorized):  # noqa: F811
    """A kernel-claimed card whose sender died before the permit (row executing, no permit)."""
    actor, config, case, verified_message, _, _ = approved_credit
    p = verified_message.structured_output["accounting_review"]
    name, params = inputs(p)
    payload = build_confirmation_payload(
        mutation_type="create",
        record_type=p["record_type"],
        tool_name=name,
        tool_input=params,
        session_id=str(verified_message.session_id),
        current_record=p["before"],
    )
    payload.accounting_review = p
    message = ChatMessage(
        tenant_id=actor.tenant_id,
        session_id=verified_message.session_id,
        role="assistant",
        content="",
        structured_output={**payload.model_dump(mode="json"), "status": "executing"},
    )
    db.add(message)
    await db.flush()
    claimed = await chat_confirmation.claim(db, actor.tenant_id, message, actor_id=actor.id)
    message.structured_output = {**message.structured_output, "operation_id": str(claimed.operation_id)}
    await db.flush()
    return actor.tenant_id, actor.id, message.id, claimed, p


async def test_an_attempt_expired_before_any_send_releases_the_intent_for_a_fresh_card(db, claimed_no_permit):
    """The same audit every other before-effect refusal writes: the cross-card history check
    releases the intent by it, and the ledger admits a lineage retry of the row."""
    from app.services.transaction_ops.resolution_plan import previous_execution

    tenant_id, actor_id, message_id, claimed, p = claimed_no_permit
    later = (await _row(db, claimed)).deadline_at + timedelta(seconds=1)
    with patch("app.services.transaction_ops.accounting_group.accounting_write_slot", _no_lock):
        assert (await mod.recover(db, tenant_id, message_id, now=later))["termination_reason"] == "done"
    row = await _row(db, claimed)
    assert row.status == "rejected_before_effect" and not state.permit_consumed(row)
    message = await db.get(ChatMessage, message_id)
    await db.refresh(message)
    so = message.structured_output
    assert so["status"] == "failed"
    assert (await chat_confirmation._retryable_attempt(db, tenant_id, so)).id == row.id
    released = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.resource_id == str(message_id),
            AuditEvent.action == "accounting_correction.precondition_failed",
        )
    )
    assert released is not None and released.payload["financial_writes"] == 0
    assert released.payload["approved_by"] == str(actor_id)
    fresh = ChatMessage(
        tenant_id=tenant_id,
        session_id=message.session_id,
        role="assistant",
        content="",
        structured_output={
            **{k: v for k, v in so.items() if k not in ("operation_id", "accounting_execution", "error")},
            "status": "pending",
        },
    )
    db.add(fresh)
    await db.flush()
    assert await previous_execution(db, tenant_id, fresh.id, p) is None


async def test_a_row_verified_while_its_card_never_heard_queues_the_recheck_like_every_verified_path(
    db, interrupted, readback
):
    tenant_id, actor_id, message_id, claimed = interrupted
    await state.complete_operation(
        db,
        tenant_id,
        claimed.operation_id,
        outcome="verified",
        result_json={"code": "independently_verified", "verification": {"status": "verified", "credit_memo_id": "31"}},
    )
    now = datetime.now(timezone.utc)
    with patch("app.services.transaction_ops.accounting_group.accounting_write_slot", _no_lock):
        assert (await mod.recover(db, tenant_id, message_id, now=now))["termination_reason"] == "done"
    message = await db.get(ChatMessage, message_id)
    await db.refresh(message)
    so = message.structured_output
    assert so["status"] == "approved" and so["accounting_verification"]["status"] == "verified"
    assert so["accounting_recheck"]["status"] == "queued"


async def test_the_scheduler_reaches_a_flagless_tenant_whose_card_sits_on_a_terminal_row(db, interrupted, monkeypatch):
    from app.services import feature_flag_service
    from app.services.transaction_ops import action_scheduler

    tenant_id, _, message_id, claimed = interrupted
    await state.complete_operation(
        db, tenant_id, claimed.operation_id, outcome="needs_review", result_json={"code": "adapter_defect"}
    )
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(feature_flag_service, "list_tenants_with_flags", AsyncMock(return_value=[]))
    publish = AsyncMock()
    monkeypatch.setattr(action_scheduler, "_dispatch", publish)
    stats = await action_scheduler.collect_due_actions(db, now)
    assert stats["credit_recoveries"] == 1
    assert publish.call_args.args[:3] == (tenant_id, "credit_recover", message_id)


async def test_a_receipted_row_the_kernel_already_closed_is_not_rendered_as_interrupted(db, interrupted, readback):
    """The card said "sent and saved, not yet verified"; a later readback that still finds no
    proof keeps that truth (approved, unverified, receipt accepted), never "interrupted"."""
    tenant_id, actor_id, message_id, claimed = interrupted
    await state.record_receipt(
        db, tenant_id, claimed.operation_id, {"status": "accepted", "verified": False, "id": "30"}
    )
    await state.complete_operation(
        db,
        tenant_id,
        claimed.operation_id,
        outcome="committed_unverified",
        result_json={"code": "verification_unproven"},
    )
    message = await db.get(ChatMessage, message_id)
    message.structured_output = {
        **message.structured_output,
        "status": "approved",
        "accounting_verification": {"status": "needs_review", "reason": "gl_mismatch"},
    }
    await db.commit()
    readback.return_value = {"status": "needs_review", "reason": "gl_mismatch", "retry_allowed": False}
    later = (await _row(db, claimed)).deadline_at + timedelta(seconds=1)
    assert await mod.candidates(db, tenant_id, later, limit=10) == [message_id]
    with patch("app.services.transaction_ops.accounting_group.accounting_write_slot", _no_lock):
        await mod.recover(db, tenant_id, message_id, now=later)
    await db.refresh(message)
    so = message.structured_output
    assert (await _row(db, claimed)).status == "committed_unverified"
    assert so["status"] == "approved" and "interrupted" not in message.content
    assert so["accounting_verification"]["receipt_outcome"] == "accepted"
    assert so["accounting_verification"]["recovered_by_read"] is False


async def test_a_recovery_that_cannot_run_hands_the_row_to_a_person_instead_of_raising_forever(
    db, interrupted, readback
):
    from sqlalchemy import update

    from app.models.transaction_ops import TransactionConfig

    tenant_id, _, message_id, claimed = interrupted
    row = await _row(db, claimed)
    config_id = UUID(row.result_json["recovery_scope"]["config_id"])
    await db.execute(
        update(TransactionConfig).where(TransactionConfig.id == config_id).values(enabled=False, schedule_enabled=False)
    )
    await db.commit()
    later = row.deadline_at + timedelta(seconds=1)
    with patch("app.services.transaction_ops.accounting_group.accounting_write_slot", _no_lock):
        result = await mod.recover(db, tenant_id, message_id, now=later)
    row = await _row(db, claimed)
    assert result["termination_reason"] == "blocked" and row.status == "needs_review"
    assert row.result_json["code"] == "config_disabled"
    assert await mod.candidates(db, tenant_id, later, limit=10) == []
    readback.assert_not_awaited()


async def test_a_row_whose_card_was_deleted_is_settled_and_not_redispatched(db, interrupted):
    from sqlalchemy import delete

    tenant_id, _, message_id, claimed = interrupted
    await db.execute(delete(ChatMessage).where(ChatMessage.id == message_id))
    await db.commit()
    later = (await _row(db, claimed)).deadline_at + timedelta(seconds=1)
    assert await mod.candidates(db, tenant_id, later, limit=10) == [message_id]
    result = await mod.recover(db, tenant_id, message_id, now=later)
    assert result["termination_reason"] == "blocked"
    row = await _row(db, claimed)
    assert row.status == "needs_review" and row.result_json["code"] == "card_missing"
    assert await mod.candidates(db, tenant_id, later + timedelta(hours=1), limit=10) == []


@pytest.fixture
async def native_interrupted(db, approved_credit, authorized):  # noqa: F811
    """A native amendment card claimed by the kernel whose sender died after the permit. Its
    proposal names the seeded config and case, so a recovery can run under them."""
    from tests.test_accounting_approval_flow import kind_proposal, native_payload

    actor, config, case, verified_message, _, _ = approved_credit
    p = kind_proposal("native_credit")
    p.update(
        tenant_id=str(actor.tenant_id),
        config_id=str(config.id),
        case_id=str(case.id),
        order_reference=case.order_reference,
        scope=case.scope_json,
    )
    message = ChatMessage(
        tenant_id=actor.tenant_id,
        session_id=verified_message.session_id,
        role="assistant",
        content="",
        structured_output={
            **native_payload(p, str(verified_message.session_id)).model_dump(mode="json"),
            "status": "executing",
        },
    )
    db.add(message)
    await db.flush()
    claimed = await chat_confirmation.claim(db, actor.tenant_id, message, actor_id=actor.id)
    message.structured_output = {**message.structured_output, "operation_id": str(claimed.operation_id)}
    await db.flush()
    assert await state.reserve_operation_dispatch(
        db,
        actor.tenant_id,
        claimed,
        provider=chat_confirmation.PROVIDER_NATIVE,
        payload_fingerprint="b" * 64,
        authorize=chat_confirmation.authorize_dispatch,
    )
    return actor.tenant_id, message.id, claimed, p


async def test_a_native_row_recovered_by_read_stores_the_readback_the_way_its_adapter_does(
    db, native_interrupted, monkeypatch
):
    """The recovery scan reads back without an adapter instance. The native readback carries
    whole records (the after snapshot, the invoice and sales order it compared) that exceed
    the ledger row's 64 KiB bound; storing it raw made every recovery pass raise and left a
    saved, verifiable amendment stuck. The row keeps the adapter's projection instead."""
    from app.services.transaction_ops.resolution_plan import operation_identity

    tenant_id, message_id, claimed, p = native_interrupted
    verification = {
        "status": "verified",
        "record_type": p["record_type"],
        "record_id": p["record_id"],
        "credit_memo_id": p["record_id"],
        "invoice": {"lines": [{"n": i, "memo": "x" * 20} for i in range(2500)]},
        "sales_order": {"lines": [{"n": i, "memo": "y" * 20} for i in range(2500)]},
        "after": {"body": {"total": "440.00", "custbody_ecom_tx_ops_work_key": operation_identity(p)}},
        "source_revision": "rev-9",
        "ledger": [{"account": "AR", "debit": "0.00", "credit": "33.80"}],
        "related_records_unchanged": True,
        "retry_allowed": False,
        "financial_writes": 0,
        "scope": "native_amendment_and_related_postings",
        "full_reconciliation_required": True,
    }
    assert len(json.dumps(verification)) > 65_536
    readback = AsyncMock(return_value=verification)
    monkeypatch.setattr("app.services.transaction_ops.native_accounting_service.verify_after", readback)
    later = (await _row(db, claimed)).deadline_at + timedelta(seconds=1)
    with patch("app.services.transaction_ops.accounting_group.accounting_write_slot", _no_lock):
        result = await mod.recover(db, tenant_id, message_id, now=later)
    assert result == {"termination_reason": "done", "financial_writes": 0}
    readback.assert_awaited_once()
    row = await _row(db, claimed)
    assert row.status == "verified" and row.provider == chat_confirmation.PROVIDER_NATIVE
    stored = row.result_json["verification"]
    assert stored["status"] == "verified" and stored["source_revision"] == "rev-9"
    assert not {"invoice", "sales_order", "after"} & set(stored)
    message = await db.get(ChatMessage, message_id)
    await db.refresh(message)
    assert message.structured_output["status"] == "approved"
    assert message.structured_output["accounting_verification"]["after"] == verification["after"]


async def test_a_crash_after_an_answer_that_named_another_record_recovers_with_that_identity(
    db, native_interrupted, monkeypatch
):
    """The live readback refuses an answer that names a different record than the approval.
    After a crash the only memory of that answer is the row, so the recovery scan reads
    back with the recorded identity instead of reconciling a different record away."""
    tenant_id, message_id, claimed, p = native_interrupted
    foreign = {"record_id": "999", "record_type": p["record_type"]}
    await state.record_dispatch_answer(db, tenant_id, claimed.operation_id, foreign)
    readback = AsyncMock(return_value={"status": "needs_review", "reason": "native_receipt_identity_conflict"})
    monkeypatch.setattr("app.services.transaction_ops.native_accounting_service.verify_after", readback)
    later = (await _row(db, claimed)).deadline_at + timedelta(seconds=1)
    with patch("app.services.transaction_ops.accounting_group.accounting_write_slot", _no_lock):
        await mod.recover(db, tenant_id, message_id, now=later)
    readback.assert_awaited_once()
    assert readback.await_args.args[3] == foreign  # tax_correction passes the receipt positionally
    row = await _row(db, claimed)
    assert row.status != "verified" and row.result_json["answer"] == foreign
    message = await db.get(ChatMessage, message_id)
    await db.refresh(message)
    assert message.structured_output["status"] != "approved"
