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
from app.services.transaction_ops import tax_correction
from app.services.transaction_ops.resolution_plan import operation_identity
from tests.test_accounting_approval_flow import inputs, kind_proposal, native_payload

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


async def approve(db, card, *, preflight=None, dispatch=None, readback=None, authorize=None, transport=None):
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
        # the native amendment RESTlet, when the card is native (the kernel adapter calls it directly)
        patch("app.services.transaction_ops.native_accounting_transport._request", transport or AsyncMock()),
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
    assert claim["recovery_scope"]["order_reference"] == p["order_reference"]
    # The completion job proves provenance by the approval-claimed audit row; a kernel
    # card writes it from the projection, with the fields that check matches on.
    claimed_audit = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.resource_id == str(message.id),
            AuditEvent.action == accounting_recovery.CLAIM_ACTION,
            AuditEvent.actor_id == actor.id,
            AuditEvent.payload["evidence_digest"].astext == claim["evidence_digest"],
            AuditEvent.payload["accepted_at"].astext == claim["accepted_at"],
        )
    )
    assert claimed_audit is not None and claimed_audit.payload["financial_writes"] == 0
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
    # an update names its record, so the link survives the recovery sentence
    assert ("[View" in _text(events)) == (so["mutation_type"] == "update")
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
    # the group link is on the card from the claim on, so recovery can refresh the parent
    assert stored.structured_output["accounting_execution"]["approval_context"]["group_approval_id"] == str(parent_id)
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


async def test_a_kernel_card_without_a_config_key_still_queues_its_recheck_under_the_recorded_config(db, card):
    """The recheck queue used to index the card's config with bracket access; a card without
    the key raised KeyError, swallowed as "not queued"."""
    from app.services.transaction_ops import accounting_recheck

    actor, session, message, p, _ = card
    p2 = dict(p)
    p2.pop("config_id", None)
    message.structured_output = {**message.structured_output, "accounting_review": p2}
    from app.services.chat.write_confirmation_service import build_confirmation_payload as _build

    name, params = message.structured_output["tool_name"], message.structured_output["tool_input"]
    payload = _build(
        mutation_type=message.structured_output["mutation_type"],
        record_type=p2["record_type"],
        tool_name=name,
        tool_input=params,
        session_id=str(session.id),
        current_record=p2["before"],
    )
    payload.accounting_review = p2
    message.structured_output = {**payload.model_dump(mode="json"), "status": "pending"}
    await db.flush()
    events, so, stubs = await approve(db, (actor, session, message, p2, params))
    assert so["status"] == "approved"
    row = await _row(db, message)
    # the orchestrator queues the recheck under the config the claim recorded
    assert stubs["recheck"].await_args.kwargs["config_id"] == row.result_json["recovery_scope"]["config_id"]
    assert accounting_recheck.effective_config_id(so) == row.result_json["recovery_scope"]["config_id"]
    # a card whose scope names no config is refused by the real queue with a code, never KeyError
    bare = {**so, "accounting_execution": {**so["accounting_execution"], "recovery_scope": {}}}
    message.structured_output = bare
    with pytest.raises(Exception) as exc:
        await accounting_recheck.queue(db, actor.tenant_id, message, actor.id, now=datetime.now(timezone.utc))
    assert not isinstance(exc.value, KeyError) and "unscoped" in str(exc.value)


async def test_a_transient_revalidation_failure_is_reported_as_itself_not_as_changed_evidence(db, card):
    events, so, stubs = await approve(
        db, card, preflight=AsyncMock(side_effect=RuntimeError("connection reset by peer"))
    )
    assert so["status"] == "failed" and stubs["dispatch"].await_count == 0
    assert "connection reset by peer" in so["error"]
    assert "no longer holds" not in so["error"]
    row = await _row(db, card[2])
    assert row.result_json["code"] == "evidence_revalidation_failed"  # the ledger keeps the code, not the text


# ── The native amendment card through the kernel ─────────────────────────────────────────


@pytest.fixture
async def native_card(db, admin_user):
    """A pending native amendment card (tool transaction_ops_accounting_amendment_apply)."""
    actor, _ = admin_user
    p = kind_proposal("native_credit")
    p["tenant_id"] = str(actor.tenant_id)
    session = ChatSession(tenant_id=actor.tenant_id, user_id=actor.id, title="approve native")
    db.add(session)
    await db.flush()
    payload = native_payload(p, str(session.id))
    params = payload.tool_input
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


def _native_receipt(p):
    return {
        "success": True,
        "schema_version": 1,
        "status": "posted_pending_independent_verification",
        "record_type": p["record_type"],
        "record_id": p["record_id"],
        "work_key": operation_identity(p),
        "financial_writes": 1,
    }


async def test_a_native_amendment_runs_through_the_kernel_end_to_end(db, native_card):
    """The last legacy card: no card-side claim, no reservation audit, no second permit. The
    RESTlet is called once behind the ledger's permit with the ledger row as its approval id."""
    actor, _, message, p, params = native_card
    transport = AsyncMock(return_value=_native_receipt(p))
    events, so, stubs = await approve(db, native_card, transport=transport)
    row = await _row(db, message)
    assert row.status == "verified" and row.provider == "netsuite_native" and row.adapter == "native_amendment"
    assert state.permit_consumed(row) and row.work_key == operation_identity(p)
    assert row.result_json["receipt"]["record_id"] == p["record_id"]
    assert transport.await_count == 1 and stubs["dispatch"].await_count == 0
    sent = transport.await_args.args[5]
    assert sent["work_key"] == operation_identity(p) and sent["approval_audit_id"] == str(row.id)
    assert so["status"] == "approved" and so["operation_id"] == str(row.id)
    claim = so["accounting_execution"]
    assert claim["operation_id"] == str(row.id) and claim["approved_by"] == str(actor.id)
    assert claim["receipt"]["record_id"] == p["record_id"] and claim["receipt"]["record_type"] == p["record_type"]
    assert "reservation_audit_id" not in claim["receipt"] and claim["attempts"] > 0
    assert so["accounting_verification"]["status"] == "verified"
    assert so["accounting_recheck"] == {"status": "queued", "run_id": "recheck-run"}
    assert p["scope"]["netsuite_account_id"].replace("_", "-").lower() in so["record_url"]
    legacy = list(
        await db.scalars(
            select(AuditEvent).where(
                AuditEvent.resource_id == str(message.id), AuditEvent.action.like("accounting.native_dispatch.%")
            )
        )
    )
    assert legacy == []
    assert stubs["preflight"].await_args.args[2:] == (so["tool_name"], params, p)
    assert stubs["readback"].await_args.kwargs["receipt"] == _native_receipt(p)
    stubs["recheck"].assert_awaited_once()
    text = _text(events)
    assert "independently verified" in text and "[View" in text


async def test_a_native_amendment_whose_evidence_changed_sends_nothing(db, native_card):
    actor, _, message, p, _ = native_card
    transport = AsyncMock(return_value=_native_receipt(p))
    events, so, stubs = await approve(
        db,
        native_card,
        preflight=AsyncMock(side_effect=ValueError("native_current_subledger_changed")),
        transport=transport,
    )
    row = await _row(db, message)
    assert row.status == "rejected_before_effect" and not state.permit_consumed(row)
    assert row.result_json["code"] == "native_current_subledger_changed"
    assert transport.await_count == 0 and stubs["readback"].await_count == 0
    assert so["status"] == "failed" and so["error"] == "native_current_subledger_changed"
    released = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.resource_id == str(message.id),
            AuditEvent.action == "accounting_correction.precondition_failed",
        )
    )
    assert released.payload["financial_writes"] == 0 and released.payload["approved_by"] == str(actor.id)


async def test_a_native_retry_after_a_refusal_sends_the_base_work_key(db, native_card):
    """A lineage retry claims a NEW work key on the ledger, but the RESTlet stamps and later
    verifies custbody_ecom_tx_ops_work_key against the business identity, so the send
    carries the base key."""
    actor, session, message, p, _ = native_card
    await approve(db, native_card, preflight=AsyncMock(side_effect=ValueError("native_approved_record_changed")))
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
    transport = AsyncMock(return_value=_native_receipt(p))
    events, so, stubs = await approve(db, (actor, session, retry_card, p, None), transport=transport)
    second = await _row(db, retry_card)
    assert second.status == "verified" and second.retry_of_operation_id == first.id
    assert second.work_key != first.work_key and second.base_work_key == first.base_work_key
    assert transport.await_args.args[5]["work_key"] == first.base_work_key == operation_identity(p)
    assert transport.await_args.args[5]["approval_audit_id"] == str(second.id)


@pytest.mark.parametrize("tamper", ["signature", "proposal", "params", "revoked", "owner", "status"])
async def test_a_tampered_native_card_never_reaches_the_restlet(db, native_card, tamper):
    """The retired dispatcher's tamper matrix, re-pinned on the kernel path: a forged token,
    a proposal or signed input edited after minting, a revoked approver, an approver who
    does not own the session, or a card no longer pending is refused before any send."""
    from uuid import uuid4

    actor, session, message, p, params = native_card
    so = message.structured_output
    preflight = authorize = None
    if tamper == "signature":
        so = {**so, "confirmation_token": "forged"}
    elif tamper == "proposal":
        so = {**so, "accounting_review": {**p, "source": {**p["source"], "total": "1.00"}}}
        preflight = tax_correction.validate_approved  # the real binding check, no reads reached
    elif tamper == "params":
        so = {**so, "tool_input": {**params, "recordId": "999"}}
        preflight = tax_correction.validate_approved
    elif tamper == "revoked":
        authorize = AsyncMock(side_effect=ValueError("approver_not_permitted"))
    elif tamper == "owner":
        session.user_id = uuid4()
        await db.flush()
    elif tamper == "status":
        so = {**so, "status": "failed"}
    message.structured_output = so
    await db.flush()
    transport = AsyncMock(return_value=_native_receipt(p))
    events, so, stubs = await approve(
        db, (actor, session, message, p, params), preflight=preflight, authorize=authorize, transport=transport
    )
    transport.assert_not_awaited()
    stubs["dispatch"].assert_not_awaited()
    stubs["readback"].assert_not_awaited()
    row = await _row(db, message)
    assert row is None or (row.status == "rejected_before_effect" and not state.permit_consumed(row))
    assert so["status"] in ("failed", "pending", "executing") and so["status"] != "approved"
    assert any(e.get("type") == "error" for e in events)


async def test_a_legacy_native_reservation_blocks_a_new_approval_even_when_its_card_is_gone(db, native_card):
    """The retired durable dispatcher reserved a work key in an audit row rather than on a
    card, and an audit row outlives the session a card lives in (deleting a session hard-
    deletes its messages). Until the legacy recovery scan goes, that audit is the only
    thing between a pre-kernel native send still in flight and a second one."""
    from app.services.audit_service import log_event
    from app.services.transaction_ops import resolution_plan

    actor, _, message, p, _ = native_card
    await log_event(
        db,
        actor.tenant_id,
        "transaction_ops",
        resolution_plan.LEGACY_NATIVE_RESERVATION,
        actor_id=actor.id,
        resource_type="chat_message",
        resource_id=str(uuid.uuid4()),  # the card this reserved has since been deleted
        payload={"operation_key": operation_identity(p), "approved_by": str(actor.id), "financial_writes": 0},
    )
    await db.commit()
    transport = AsyncMock(return_value=_native_receipt(p))
    events, so, stubs = await approve(db, native_card, transport=transport)
    transport.assert_not_awaited()
    stubs["dispatch"].assert_not_awaited()
    assert await _row(db, message) is None  # nothing was even claimed on the ledger
    assert so["status"] == "pending"
    assert any("already has an execution record" in e.get("error", "") for e in events if e.get("type") == "error")
