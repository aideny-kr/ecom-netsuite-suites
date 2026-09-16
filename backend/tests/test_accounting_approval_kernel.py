"""G3.2b: an approved accounting card runs through the write kernel (real database).

The approve branch claims the card on the operation ledger, runs the treatment's
preflight, the signed dispatcher (behind the one-use permit) and the readback through
the kernel, and renders the card from the ledger's outcome. These tests pin, per
treatment and per provider answer, the ledger row, the card and the audit trail. The
treatment's own reads and the dispatcher are stubbed: what is under test is the seam.
"""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.chat import ChatMessage, ChatSession
from app.models.transaction_ops import TransactionOperation
from app.services.chat.orchestrator import run_chat_turn
from app.services.chat.write_confirmation_service import build_confirmation_payload
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.resolution_plan import operation_identity
from tests.test_accounting_approval_flow import inputs, kind_proposal

KINDS = ("tax", "credit", "discount", "sales_order", "api_credit")


@pytest.fixture(params=KINDS)
def kind(request):
    return request.param


@pytest.fixture
async def card(db, admin_user, kind):
    """A pending accounting card in a session the approver owns."""
    actor, _ = admin_user
    p = kind_proposal(kind)
    p["tenant_id"] = str(actor.tenant_id)
    name, params = inputs(p)
    session = ChatSession(tenant_id=actor.tenant_id, user_id=actor.id, title="approve")
    db.add(session)
    await db.flush()
    payload = build_confirmation_payload(
        mutation_type="create" if kind == "credit" else "update",
        record_type=p["record_type"],
        tool_name=name,
        tool_input=params,
        session_id=str(session.id),
        current_record=p["before"],
    )
    payload.accounting_review = p
    message = ChatMessage(
        tenant_id=actor.tenant_id,
        session_id=session.id,
        role="assistant",
        content="",
        structured_output={**payload.model_dump(mode="json"), "status": "pending"},
        created_at=datetime.now(timezone.utc),
    )
    db.add(message)
    await db.flush()
    return actor, session, message, p, params


@asynccontextmanager
async def _slot(_):
    yield


async def approve(db, card, *, preflight=None, dispatch=None, readback=None, authorize=None):
    actor, session, message, _, _ = card
    preflight = preflight or AsyncMock()
    dispatch = dispatch or AsyncMock(return_value=json.dumps({"success": True, "id": "30"}))
    readback = readback or AsyncMock(return_value={"status": "verified", "cash_settlement": "not_verified"})
    authorize = authorize or AsyncMock()
    recheck = AsyncMock(return_value=MagicMock(id="recheck-run"))
    with (
        patch("app.services.transaction_ops.accounting_group.accounting_write_slot", _slot),
        patch("app.services.transaction_ops.accounting_group.authorize_accounting_write", AsyncMock()),
        patch("app.services.transaction_ops.chat_confirmation.authorize_accounting_write", authorize),
        patch("app.services.transaction_ops.tax_correction.validate_approved", preflight),
        patch("app.services.transaction_ops.tax_correction.verify_after", readback),
        patch("app.services.transaction_ops.accounting_recheck.queue", recheck),
        patch("app.services.chat.orchestrator.execute_tool_call", dispatch),
        # the record link resolves the account from the connector that executed the write
        patch(
            "app.services.mcp_connector_service.get_mcp_connector",
            AsyncMock(return_value=MagicMock(provider="netsuite_mcp", metadata_json={"account_id": "123456_SB1"})),
        ),
    ):
        events = [
            e
            async for e in run_chat_turn(
                db=db,
                session=session,
                user_message="approve",
                user_id=actor.id,
                tenant_id=actor.tenant_id,
                write_confirm={"action": "approve", "confirmation_id": str(message.id)},
            )
        ]
    await db.refresh(message)
    return (
        events,
        message.structured_output,
        {"preflight": preflight, "dispatch": dispatch, "readback": readback, "recheck": recheck},
    )


async def _row(db, message):
    return await db.scalar(
        select(TransactionOperation).where(
            TransactionOperation.approval_kind == "chat_confirmation", TransactionOperation.approval_id == message.id
        )
    )


def _text(events):
    return " ".join(e["message"]["content"] for e in events if e.get("type") == "message")


async def test_a_verified_correction_is_one_ledger_row_one_send_and_a_queued_recheck(db, card):
    actor, _, message, p, params = card
    events, so, stubs = await approve(db, card)
    row = await _row(db, message)
    assert row.status == "verified" and row.work_key == operation_identity(p)
    assert state.permit_consumed(row) and row.result_json["receipt"]["id"] == "30"
    assert so["status"] == "approved" and so["operation_id"] == str(row.id)
    assert so["accounting_verification"]["status"] == "verified"
    assert so["accounting_recheck"] == {"status": "queued", "run_id": "recheck-run"}
    # The ledger row is the claim; the card carries a projection of it (version 1, attempts
    # spent) so the completion, history and group readers that read the card's claim keep
    # working, while the legacy recovery scan never picks a kernel card up.
    from app.services.transaction_ops import accounting_history, accounting_recovery

    claim = so["accounting_execution"]
    assert claim["operation_id"] == str(row.id) and claim["approved_by"] == str(actor.id)
    assert claim["operation_key"] == row.base_work_key and claim["receipt"] == {"id": "30"}
    assert claim["termination_reason"] == "done" and claim["ledger_status"] == "verified"
    assert accounting_history._claim(message) == claim
    assert not accounting_recovery.eligible(so, datetime.now(timezone.utc) + timedelta(days=1))
    assert "[View" in _text(events) and "/app/" in _text(events)  # the record link, as before
    assert stubs["dispatch"].await_count == 1
    sent = stubs["dispatch"].await_args.kwargs
    assert sent["human_approved"] is True and sent["tool_input"] == params
    assert stubs["preflight"].await_args.args[2:] == (so["tool_name"], params, p)
    assert stubs["readback"].await_args.kwargs["receipt"] == {"success": True, "id": "30"}
    stubs["recheck"].assert_awaited_once()
    text = _text(events)
    assert "independently" in text and "verified" in text
    verification = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.resource_id == p["case_id"],
            AuditEvent.action == "accounting_correction.verification.completed",
        )
    )
    assert verification.payload["approved_by"] == str(actor.id)
    assert verification.payload["before"] == p["before"]


async def test_changed_evidence_sends_nothing_and_releases_the_intent(db, card):
    actor, _, message, p, _ = card
    events, so, stubs = await approve(db, card, preflight=AsyncMock(side_effect=ValueError("NetSuite amount changed")))
    row = await _row(db, message)
    assert row.status == "rejected_before_effect" and not state.permit_consumed(row)
    assert row.result_json["code"] == "evidence_revalidation_failed"
    assert so["status"] == "failed" and so["error"] == "NetSuite amount changed"
    assert stubs["dispatch"].await_count == 0 and stubs["readback"].await_count == 0
    assert any(e.get("type") == "error" and "No change was sent" in e["error"] for e in events)
    released = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.resource_id == str(message.id),
            AuditEvent.action == "accounting_correction.precondition_failed",
        )
    )
    assert released.payload["financial_writes"] == 0 and released.payload["reason"] == "NetSuite amount changed"


async def test_a_provider_rejection_is_terminal_with_fresh_evidence_required(db, card):
    _, _, message, _, _ = card
    events, so, stubs = await approve(db, card, dispatch=AsyncMock(return_value=json.dumps({"error": "Period locked"})))
    row = await _row(db, message)
    assert row.status == "rejected_before_effect" and state.permit_consumed(row)
    assert row.result_json["code"] == "provider_rejected_without_save"
    assert so["status"] == "failed" and so["error"] == "Period locked"
    assert so["repair_exit_reason"] == "fresh_accounting_evidence_required"
    assert stubs["readback"].await_count == 0


async def test_an_unverified_write_is_committed_unverified_and_the_card_says_review(db, card):
    _, _, message, _, _ = card
    events, so, stubs = await approve(
        db, card, readback=AsyncMock(return_value={"status": "needs_review", "reason": "gl_mismatch"})
    )
    row = await _row(db, message)
    assert row.status == "committed_unverified" and row.result_json["receipt"]["id"] == "30"
    assert so["status"] == "approved" and so["accounting_verification"]["status"] == "needs_review"
    assert "not verified" in _text(events) and "executed successfully" not in _text(events)
    stubs["recheck"].assert_not_awaited()


@pytest.mark.parametrize("raw", [json.dumps({"outcome_indeterminate": True, "error": "timeout"}), "unreadable receipt"])
async def test_an_indeterminate_send_verified_by_reads_is_recovered(db, card, raw):
    _, _, message, _, _ = card
    events, so, stubs = await approve(db, card, dispatch=AsyncMock(return_value=raw))
    row = await _row(db, message)
    assert row.status == "verified" and "receipt" not in row.result_json
    assert so["status"] == "approved" and so["accounting_verification"]["recovered_by_read"] is True
    assert "verified using fresh NetSuite reads" in _text(events)
    stubs["recheck"].assert_awaited_once()


async def test_an_indeterminate_send_without_proof_stays_unknown_for_recovery(db, card):
    _, _, message, _, _ = card
    events, so, stubs = await approve(
        db,
        card,
        dispatch=AsyncMock(return_value=json.dumps({"outcome_indeterminate": True})),
        readback=AsyncMock(return_value={"status": "needs_review", "reason": "credit_not_found"}),
    )
    row = await _row(db, message)
    assert row.status == "unknown" and state.permit_consumed(row)
    assert so["status"] == "indeterminate" and so["accounting_verification"]["recovered_by_read"] is False
    assert so["accounting_verification"]["receipt_outcome"] == "indeterminate"
    assert "Do not repeat this write" in _text(events)
    stubs["recheck"].assert_not_awaited()


async def test_a_second_approval_of_the_same_work_sends_nothing(db, card):
    actor, session, message, p, _ = card
    await approve(db, card)
    twin = ChatMessage(
        tenant_id=actor.tenant_id,
        session_id=session.id,
        role="assistant",
        content="",
        structured_output={**message.structured_output, "status": "pending"},
    )
    twin.structured_output.pop("operation_id", None)
    twin.structured_output.pop("accounting_verification", None)
    db.add(twin)
    await db.flush()
    dispatch = AsyncMock()
    events, so, _ = await approve(db, (actor, session, twin, p, None), dispatch=dispatch)
    assert dispatch.await_count == 0
    # The cross-card history sees the first card's projected claim and refuses before the
    # CAS, so the twin stays pending and untouched; the ledger would have refused too.
    assert so["status"] == "pending"
    assert await _row(db, twin) is None
    assert any("No duplicate update was sent" in e.get("error", "") for e in events if e.get("type") == "error")


async def test_a_card_edited_after_the_claim_is_refused_at_the_permit(db, card, monkeypatch):
    """Between the claim and the send the stored card must still be the one claimed."""
    _, _, message, _, _ = card
    from app.services.transaction_ops import chat_confirmation

    original = chat_confirmation.authorize_dispatch

    async def tampered(db_, tenant_id, operation, claimed, now):
        stored = await db_.get(ChatMessage, message.id)
        stored.structured_output = {**stored.structured_output, "confirmation_token": "forged"}
        await db_.flush()
        return await original(db_, tenant_id, operation, claimed, now)

    monkeypatch.setattr(chat_confirmation, "authorize_dispatch", tampered)
    events, so, stubs = await approve(db, card)
    row = await _row(db, message)
    assert row.status == "rejected_before_effect" and row.result_json["code"] == "confirmation_changed"
    assert stubs["dispatch"].await_count == 0
    assert so["status"] == "failed" and "changed after it was accepted" in so["error"]


async def test_an_approver_revoked_after_the_claim_is_refused_at_the_permit(db, card):
    """The approver is re-checked when the permit is minted, after the treatment's own
    reads: a revocation in that window sends nothing and the card says so."""
    _, _, message, _, _ = card
    authorize = AsyncMock(side_effect=[None, ValueError("permission_denied")])
    events, so, stubs = await approve(db, card, authorize=authorize)
    row = await _row(db, message)
    assert row.status == "rejected_before_effect" and row.result_json["code"] == "approval_not_authorized"
    assert not state.permit_consumed(row) and stubs["dispatch"].await_count == 0
    assert authorize.await_count == 2  # at the claim, and again at the permit
    assert so["status"] == "failed" and "no longer permitted" in so["error"]
    assert any(e.get("type") == "error" and "No change was sent" in e["error"] for e in events)


async def test_a_cancelled_group_child_keeps_its_claim_and_the_exact_approval_link(db, admin_user):
    """A group child approved through the kernel carries the server's group context into
    the dispatcher's audit; cancelling it mid-send leaves the ledger row executing with
    its permit consumed and the card executing, for recovery to read."""
    import asyncio

    from app.services.chat import external_tool_audit, tools
    from app.services.transaction_ops import accounting_group as group

    actor, _ = admin_user
    p = kind_proposal("tax")
    p["tenant_id"] = str(actor.tenant_id)
    name, params = inputs(p)
    session = ChatSession(tenant_id=actor.tenant_id, user_id=actor.id, title="group")
    db.add(session)
    await db.flush()
    payload = build_confirmation_payload(
        mutation_type="update",
        record_type=p["record_type"],
        tool_name=name,
        tool_input=params,
        session_id=str(session.id),
        current_record=p["before"],
    )
    payload.accounting_review = p
    so = {**payload.model_dump(mode="json"), "status": "pending", "accounting_group_child": True}
    child = ChatMessage(
        tenant_id=actor.tenant_id, session_id=session.id, role="assistant", content="", structured_output=so
    )
    db.add(child)
    await db.flush()
    parent_id, manifest = uuid.uuid4(), "m" * 64
    db.info["accounting_group_execution"] = {
        "confirmation_id": str(child.id),
        "session_id": str(session.id),
        "tenant_id": str(actor.tenant_id),
        "action": "approve",
        "card_digest": group.digest(so),
        "group_approval_id": str(parent_id),
        "manifest_digest": manifest,
    }
    started = asyncio.Event()

    async def hanging(*args, **kwargs):
        started.set()
        await asyncio.Future()

    tool_audit = AsyncMock()
    with (
        patch("app.services.transaction_ops.accounting_group.accounting_write_slot", _slot),
        patch("app.services.transaction_ops.accounting_group.authorize_accounting_write", AsyncMock()),
        patch("app.services.transaction_ops.chat_confirmation.authorize_accounting_write", AsyncMock()),
        patch("app.services.transaction_ops.tax_correction.validate_approved", AsyncMock()),
        patch("app.services.chat.orchestrator.execute_tool_call", tools.execute_tool_call),
        patch.object(tools, "_execute_external_tool", hanging),
        patch.object(external_tool_audit, "append_event", tool_audit),
        patch("app.services.mcp_connector_service.get_mcp_connector", AsyncMock(return_value=None)),
    ):

        async def run():
            async for _ in run_chat_turn(
                db=db,
                session=session,
                user_message="",
                user_id=actor.id,
                tenant_id=actor.tenant_id,
                write_confirm={"action": "approve", "confirmation_id": str(child.id)},
            ):
                pass

        task = asyncio.create_task(run())
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    db.info.pop("accounting_group_execution", None)
    row = await _row(db, child)
    assert row.status == "executing" and state.permit_consumed(row) and row.surface == "group"
    stored = await db.get(ChatMessage, child.id)
    await db.refresh(stored)
    assert stored.structured_output["status"] == "executing"
    assert stored.structured_output["operation_id"] == str(row.id)
    assert [c.kwargs["action"] for c in tool_audit.await_args_list] == ["tool.requested", "tool.interrupted"]
    requested = tool_audit.await_args_list[0].kwargs
    assert requested["payload"]["approval"] == {
        "confirmation_id": str(child.id),
        "group_approval_id": str(parent_id),
        "manifest_digest": manifest,
    }
    assert tool_audit.await_args_list[1].kwargs["payload"]["approval"] == requested["payload"]["approval"]


async def test_a_legacy_execution_record_on_another_card_still_blocks_the_send(db, card):
    """During the cutover, work sent under the old claim (accounting_execution on an earlier
    card, no ledger row) must still be seen: the kernel path keeps the cross-card history
    check and refuses before claiming."""
    from app.services.transaction_ops.accounting_recovery import execution_claim

    actor, session, message, p, _ = card
    earlier = ChatMessage(
        tenant_id=actor.tenant_id,
        session_id=session.id,
        role="assistant",
        content="",
        structured_output=execution_claim(
            {**message.structured_output, "status": "approved"},
            uuid.uuid4(),
            actor.id,
            {},
            now=datetime.now(timezone.utc),
        ),
    )
    db.add(earlier)
    await db.flush()
    dispatch = AsyncMock()
    events, so, _ = await approve(db, card, dispatch=dispatch)
    assert dispatch.await_count == 0
    assert await _row(db, message) is None
    assert any("No duplicate update was sent" in e.get("error", "") for e in events if e.get("type") == "error")
    assert so["status"] == "pending"  # refused before the claim: the card is untouched


async def test_a_refusal_before_any_effect_can_be_retried_as_a_lineage_row(db, card):
    """A rejected_before_effect attempt permits one more approval of the same work: the new
    row keeps the business identity (base_work_key) and names the attempt it retries."""
    actor, session, message, p, _ = card
    events, so, _ = await approve(db, card, preflight=AsyncMock(side_effect=ValueError("NetSuite amount changed")))
    first = await _row(db, message)
    assert first.status == "rejected_before_effect"
    retry_card = ChatMessage(
        tenant_id=actor.tenant_id,
        session_id=session.id,
        role="assistant",
        content="",
        structured_output={**message.structured_output, "status": "pending"},
    )
    for key in ("operation_id", "error"):
        retry_card.structured_output.pop(key, None)
    db.add(retry_card)
    await db.flush()
    events, so, stubs = await approve(db, (actor, session, retry_card, p, None))
    second = await _row(db, retry_card)
    assert second is not None and second.status == "verified"
    assert second.retry_of_operation_id == first.id
    assert second.base_work_key == first.base_work_key == first.work_key
    assert second.work_key != first.work_key
    assert stubs["dispatch"].await_count == 1
    third = ChatMessage(
        tenant_id=actor.tenant_id,
        session_id=session.id,
        role="assistant",
        content="",
        structured_output={**retry_card.structured_output, "status": "pending"},
    )
    for key in ("operation_id", "accounting_verification", "accounting_recheck"):
        third.structured_output.pop(key, None)
    db.add(third)
    await db.flush()
    events, so, _ = await approve(db, (actor, session, third, p, None), dispatch=AsyncMock())
    assert so["status"] == "pending" and await _row(db, third) is None
    assert any("No duplicate update was sent" in e.get("error", "") for e in events if e.get("type") == "error")


async def test_a_verified_kernel_write_can_be_completed_by_the_recheck_pipeline(db, card):
    """accounting_completion.enqueue reads the card's claim; a kernel-claimed card must
    enqueue exactly like a legacy one (gate round two: it silently no-op'd before)."""
    from types import SimpleNamespace

    from app.services.transaction_ops import accounting_completion

    _, _, message, _, _ = card
    await approve(db, card)
    await db.refresh(message)
    run = SimpleNamespace(id=uuid.uuid4())
    accounting_completion.enqueue(message, run, datetime.now(timezone.utc))
    completion = message.structured_output.get("accounting_completion")
    assert completion and completion["run_id"] == str(run.id) and completion["status"] == "pending"


async def test_a_redelivery_carries_the_receipt_the_ledger_recorded(db, card):
    """A second delivery whose permit is refused does not lose the first delivery's
    receipt: the adapter reads it from the row, so the audit and the card show it."""
    _, _, message, _, _ = card
    await approve(db, card, readback=AsyncMock(return_value={"status": "needs_review", "reason": "gl_mismatch"}))
    row = await _row(db, message)
    assert row.status == "committed_unverified"
    from app.services.transaction_ops import accounting_adapter

    adapter = accounting_adapter.AccountingCardAdapter(
        name="x",
        message=message,
        tool_name=message.structured_output["tool_name"],
        tool_input=message.structured_output["tool_input"],
        actor_id="a",
        session_id=str(message.session_id),
        correlation_id="c",
        validate=AsyncMock(),
        dispatch=AsyncMock(),
        readback=AsyncMock(),
    )
    from app.schemas.transaction_runs import ClaimedOperation
    from app.services.transaction_ops.chat_confirmation import claim as _claim_card  # noqa: F401

    claimed = ClaimedOperation(
        operation_id=row.id,
        proposal_id=None,
        approval_kind="chat_confirmation",
        approval_id=message.id,
        work_key=row.work_key,
        config_id=None,
        action="x",
        currency=None,
        netsuite_account_id="a",
        subsidiary_id="s",
        record_type="r",
        target_record_id=None,
        before_json={},
        after_json={},
    )
    receipt = await adapter.send(db, message.tenant_id, claimed, {})
    assert receipt["status"] == "unknown" and receipt["code"] == "dispatch_already_reserved"
    assert adapter.receipt == {"status": "accepted", "verified": False, "id": "30"}
    assert adapter.dispatch.await_count == 0
