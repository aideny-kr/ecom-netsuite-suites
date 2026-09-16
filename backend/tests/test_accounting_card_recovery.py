"""G3.2c: a card the write kernel claimed is recovered from the ledger, by reads only.

The process that sent the write may die between the permit and the readback. The ledger
row says what is known (executing with a consumed permit; or a receipt); after the row's
deadline the recovery scan finds it, settles it, reads the provider once under its own
budget, and only a proof moves it to verified. The card is rendered from the row.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

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


from contextlib import asynccontextmanager  # noqa: E402


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
    return actor.tenant_id, message.id


async def test_a_card_that_died_before_its_ledger_claim_is_released_after_the_grace_period(db, orphan):
    """No ledger row means no permit was ever minted, so nothing was sent: after the grace
    period the card is released (failed, with the reason) and the intent is free again."""
    tenant_id, message_id = orphan
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
