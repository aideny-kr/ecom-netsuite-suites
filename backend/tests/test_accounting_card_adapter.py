"""G3.2b: an accounting card's treatment runs through the write kernel.

The adapter carries the treatment's revalidation, the signed dispatcher and the readback;
the kernel decides the ledger outcome. These tests pin which provider answer lands in
which ledger state for a chat claim, that the permit is consulted before every send, and
that the card keeps the human-readable reason the ledger deliberately does not.
"""

import json
from unittest.mock import AsyncMock

import pytest

from app.services.transaction_ops import accounting_adapter, chat_confirmation, write_kernel
from app.services.transaction_ops import state_service as state
from tests.test_accounting_recheck import approved_credit  # noqa: F401
from tests.test_accounting_recovery import interrupted_credit  # noqa: F401
from tests.test_chat_confirmation_source import _bind_card, _row, authorized  # noqa: F401


def build(message, *, validate=None, dispatch=None, readback=None):
    so = message.structured_output
    return accounting_adapter.AccountingCardAdapter(
        name=chat_confirmation.adapter_of(so["accounting_review"]),
        message=message,
        tool_name=so["tool_name"],
        tool_input=so["tool_input"],
        actor_id="actor",
        session_id=str(message.session_id),
        correlation_id="corr",
        validate=validate or AsyncMock(),
        dispatch=dispatch or AsyncMock(return_value=json.dumps({"success": True, "id": "30"})),
        readback=readback or AsyncMock(return_value={"status": "verified", "credit_memo_id": "30"}),
        approval_context={"confirmation_id": str(message.id)},
    )


@pytest.fixture
async def claimed(db, interrupted_credit, authorized):  # noqa: F811
    actor, _, _, message, _, _ = interrupted_credit
    claim = await chat_confirmation.claim(db, actor.tenant_id, message, actor_id=actor.id)
    await _bind_card(db, message, claim)
    return actor, message, claim


async def _run(db, claimed, adapter):
    actor, _, claim = claimed
    result = await write_kernel.execute(db, actor.tenant_id, claim, adapter)
    return result, await _row(db, claim)


async def test_a_verified_write_is_one_dispatch_behind_the_permit(db, claimed):
    _, message, _ = claimed
    adapter = build(message)
    result, row = await _run(db, claimed, adapter)
    assert result["status"] == "verified" and row.status == "verified"
    assert state.permit_consumed(row) and row.result_json["receipt"] == {
        "status": "accepted",
        "verified": False,
        "id": "30",
    }
    assert row.result_json["verification"] == {"status": "verified", "credit_memo_id": "30"}
    assert adapter.dispatch.await_count == 1
    sent = adapter.dispatch.await_args.kwargs
    assert sent["human_approved"] is True and sent["tool_input"] == message.structured_output["tool_input"]
    assert adapter.validate.await_args.args[2:] == (adapter.tool_name, adapter.tool_input, adapter.proposal)
    assert adapter.readback.await_args.kwargs["receipt"] == {"success": True, "id": "30"}
    assert adapter.verification["status"] == "verified"
    assert row.api_calls_used == accounting_adapter.PREFLIGHT_CALLS + 1 + accounting_adapter.VERIFY_CALLS


@pytest.mark.parametrize(
    "reason,code",
    [
        ("credit_api_source_changed", "credit_api_source_changed"),
        ("This tax update needs a fresh, evidence-bound approval card.", "evidence_revalidation_failed"),
    ],
)
async def test_a_changed_precondition_sends_nothing_and_keeps_the_reason_for_the_card(db, claimed, reason, code):
    _, message, _ = claimed
    adapter = build(message, validate=AsyncMock(side_effect=ValueError(reason)))
    result, row = await _run(db, claimed, adapter)
    assert result["status"] == "rejected_before_effect" and row.result_json["code"] == code
    assert not state.permit_consumed(row)
    assert adapter.dispatch.await_count == 0
    assert adapter.refusal == reason
    assert reason not in json.dumps(row.result_json) or code == reason


async def test_a_card_that_changed_after_the_claim_is_refused_at_the_permit(db, claimed):
    _, message, _ = claimed
    message.structured_output = {**message.structured_output, "status": "failed"}
    await db.flush()
    adapter = build(message)
    result, row = await _run(db, claimed, adapter)
    assert result["status"] == "rejected_before_effect" and row.result_json["code"] == "confirmation_changed"
    assert adapter.dispatch.await_count == 0 and not state.permit_consumed(row)
    assert adapter.refusal == accounting_adapter.REFUSALS["confirmation_changed"]


async def test_a_provider_rejection_is_before_effect_with_the_permit_spent(db, claimed):
    _, message, _ = claimed
    adapter = build(message, dispatch=AsyncMock(return_value=json.dumps({"error": "HTTP 400: Period locked"})))
    result, row = await _run(db, claimed, adapter)
    assert result["status"] == "rejected_before_effect"
    assert row.result_json["code"] == "provider_rejected_without_save" and state.permit_consumed(row)
    assert adapter.refusal == "HTTP 400: Period locked"
    assert adapter.readback.await_count == 0


@pytest.mark.parametrize("raw", [json.dumps({"outcome_indeterminate": True, "error": "timeout"}), "unreadable receipt"])
async def test_an_indeterminate_send_is_recovered_only_by_the_readback(db, claimed, raw):
    _, message, _ = claimed
    adapter = build(message, dispatch=AsyncMock(return_value=raw))
    result, row = await _run(db, claimed, adapter)
    assert result["status"] == "verified"
    assert "receipt" not in row.result_json  # nothing the provider said was trusted
    assert row.result_json["verification"]["status"] == "verified"


async def test_an_indeterminate_send_without_proof_is_unknown(db, claimed):
    _, message, _ = claimed
    adapter = build(
        message,
        dispatch=AsyncMock(return_value=json.dumps({"outcome_indeterminate": True})),
        readback=AsyncMock(return_value={"status": "needs_review", "reason": "credit_not_found"}),
    )
    result, row = await _run(db, claimed, adapter)
    assert result["status"] == "unknown" and row.result_json["code"] == "verification_unproven"
    assert adapter.verification == {"status": "needs_review", "reason": "credit_not_found"}


async def test_an_accepted_write_without_proof_is_committed_unverified_with_its_receipt(db, claimed):
    _, message, _ = claimed
    adapter = build(
        message,
        dispatch=AsyncMock(return_value=json.dumps({"success": True, "recordId": 30, "total": 12.5})),
        readback=AsyncMock(return_value={"status": "needs_review", "reason": "gl_mismatch", "total": 12.5}),
    )
    result, row = await _run(db, claimed, adapter)
    assert result["status"] == "committed_unverified"
    assert row.result_json["receipt"] == {"status": "accepted", "verified": False, "recordId": "30"}
    assert "verification" not in row.result_json
    assert adapter.receipt == {"success": True, "recordId": 30, "total": 12.5}


async def test_a_failed_readback_after_a_receipt_stays_committed_unverified(db, claimed):
    _, message, _ = claimed
    adapter = build(message, readback=AsyncMock(side_effect=RuntimeError("read timed out")))
    result, row = await _run(db, claimed, adapter)
    assert result["status"] == "committed_unverified" and row.result_json["code"] == "verification_unavailable"


async def test_a_second_delivery_never_dispatches_again(db, claimed):
    _, message, _ = claimed
    first = build(message)
    assert (await _run(db, claimed, first))[0]["status"] == "verified"
    second = build(message)
    result, _ = await _run(db, claimed, second)
    assert result["status"] == "verified" and second.dispatch.await_count == 0


def test_the_ledger_copy_of_a_readback_has_no_floats():
    assert accounting_adapter.ledger_safe({"gl": [{"debit": 167.07}], "n": 1, "s": "x"}) == {
        "gl": [{"debit": "167.07"}],
        "n": 1,
        "s": "x",
    }


@pytest.mark.parametrize("proved", [True, False])
async def test_a_rejection_that_names_a_saved_record_is_decided_by_the_readback(db, claimed, proved):
    """An error message beside a record id is not proof of no effect; the readback decides."""
    _, message, _ = claimed
    adapter = build(
        message,
        dispatch=AsyncMock(return_value=json.dumps({"error": "warning: partial", "id": "30"})),
        readback=AsyncMock(
            return_value={"status": "verified"} if proved else {"status": "needs_review", "reason": "x"}
        ),
    )
    result, row = await _run(db, claimed, adapter)
    assert result["status"] == ("verified" if proved else "unknown")
    assert adapter.sent == "unknown" and adapter.readback.await_count == 1


async def test_the_ledger_row_records_where_the_attempt_spent_its_time(db, claimed):
    """The post-mortem could not attribute about a minute per correction. Every attempt now
    stamps its phase boundaries on the row with the outcome, in order."""
    _, message, _ = claimed
    result, row = await _run(db, claimed, build(message))
    assert result["status"] == "verified"
    timing = row.result_json["timing"]
    order = [
        "preflight_started_at",
        "preflight_ended_at",
        "sent_at",
        "receipt_recorded_at",
        "verify_started_at",
        "verify_ended_at",
    ]
    # jsonb keeps no key order; the stamps themselves must be in phase order.
    assert set(timing) == set(order)
    stamps = [timing[key] for key in order]
    assert stamps == sorted(stamps)
    assert row.result_json["dispatch_reserved_at"] <= timing["sent_at"]


async def test_a_refusal_before_the_permit_still_records_its_timing(db, claimed):
    _, message, _ = claimed
    adapter = build(message, validate=AsyncMock(side_effect=ValueError("credit_api_source_changed")))
    result, row = await _run(db, claimed, adapter)
    assert result["status"] == "rejected_before_effect"
    assert set(row.result_json["timing"]) == {"preflight_started_at"}


# ── The native amendment card (RESTlet customscript_ecom_acct_amend) ─────────────────────
#
# The native card's adapter runs the native service's FULL preflight before the permit,
# calls the amendment RESTlet directly behind the ledger's one-use permit (the old durable
# dispatcher minted a second permit of its own), names the ledger row as the RESTlet's
# approval_audit_id, and classifies the answer with the dispatcher's confirmed/refused
# predicates: only an answer that proves THIS work (record, work key, one financial write)
# is a receipt.

from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest.mock import patch  # noqa: E402

from sqlalchemy import select  # noqa: E402

from app.models.audit import AuditEvent  # noqa: E402
from app.models.chat import ChatMessage, ChatSession  # noqa: E402
from app.services.transaction_ops.resolution_plan import operation_identity  # noqa: E402
from tests.test_accounting_approval_flow import kind_proposal, native_payload  # noqa: E402

TRANSPORT = "app.services.transaction_ops.native_accounting_transport._request"
LEGACY_RESERVATION = "accounting.native_dispatch.reserved"


def confirmed(p):
    return {
        "success": True,
        "schema_version": 1,
        "status": "posted_pending_independent_verification",
        "record_type": p["record_type"],
        "record_id": p["record_id"],
        "work_key": operation_identity(p),
        "financial_writes": 1,
    }


def refused(reason="expected_before_mismatch"):
    return {"success": False, "status": "not_submitted", "error": reason, "financial_writes": 0}


@pytest.fixture
async def native_claimed(db, admin_user, authorized):  # noqa: F811
    """A native amendment card, CAS-accepted and claimed on the ledger (provider netsuite_native)."""
    actor, _ = admin_user
    p = kind_proposal("native_credit")
    p["tenant_id"] = str(actor.tenant_id)
    session = ChatSession(tenant_id=actor.tenant_id, user_id=actor.id, title="native")
    db.add(session)
    await db.flush()
    payload = native_payload(p, str(session.id))
    message = ChatMessage(
        tenant_id=actor.tenant_id,
        session_id=session.id,
        role="assistant",
        content="",
        structured_output={**payload.model_dump(mode="json"), "status": "executing"},
        created_at=datetime.now(timezone.utc),
    )
    db.add(message)
    await db.flush()
    claim = await chat_confirmation.claim(db, actor.tenant_id, message, actor_id=actor.id)
    await _bind_card(db, message, claim)
    return actor, message, claim


def build_native(message, *, validate=None, readback=None):
    so = message.structured_output
    p = so["accounting_review"]
    return accounting_adapter.NativeAmendmentAdapter(
        name=chat_confirmation.adapter_of(p),
        message=message,
        tool_name=so["tool_name"],
        tool_input=so["tool_input"],
        actor_id="actor",
        session_id=str(message.session_id),
        correlation_id="corr",
        validate=validate or AsyncMock(),
        readback=readback
        or AsyncMock(return_value={"status": "verified", "record_type": p["record_type"], "record_id": p["record_id"]}),
        approval_context={"confirmation_id": str(message.id)},
    )


async def _legacy_reservations(db, message):
    return list(
        await db.scalars(
            select(AuditEvent).where(AuditEvent.action == LEGACY_RESERVATION, AuditEvent.resource_id == str(message.id))
        )
    )


async def test_a_native_amendment_is_sent_once_behind_the_permit_and_names_its_ledger_row(db, native_claimed):
    actor, message, claim = native_claimed
    p = message.structured_output["accounting_review"]
    before = datetime.now(timezone.utc)
    adapter = build_native(message)
    with patch(TRANSPORT, AsyncMock(return_value=confirmed(p))) as transport:
        result, row = await _run(db, native_claimed, adapter)
    assert result["status"] == "verified" and row.status == "verified"
    assert row.provider == chat_confirmation.PROVIDER_NATIVE and row.adapter == "native_amendment"
    assert state.permit_consumed(row)
    assert transport.await_count == 1
    args = transport.await_args.args
    assert args[2:5] == (p["connection_id"], p["scope"]["netsuite_account_id"], "apply")
    sent = args[5]
    assert set(sent) == {"request", "expected_before", "work_key", "approval_expires_at", "approval_audit_id"}
    assert sent["request"] == p["native_request"]
    assert sent["expected_before"] == p["native_preview"]["beforeSnapshot"]
    assert sent["work_key"] == operation_identity(p) == row.base_work_key
    # The RESTlet logs approval_audit_id in NetSuite's own audit: it is the ledger row.
    assert sent["approval_audit_id"] == str(claim.operation_id)
    expires = datetime.fromisoformat(sent["approval_expires_at"])
    assert before + timedelta(minutes=1) < expires <= datetime.now(timezone.utc) + timedelta(minutes=2)
    assert row.result_json["receipt"] == {
        "status": "accepted",
        "verified": False,
        "record_type": p["record_type"],
        "record_id": p["record_id"],
        "work_key": operation_identity(p),
    }
    assert "reservation_audit_id" not in json.dumps(row.result_json)
    assert await _legacy_reservations(db, message) == []
    # The full native preflight ran before the permit, on the adapter's validate seam.
    assert adapter.validate.await_args.args[2:] == (adapter.tool_name, adapter.tool_input, adapter.proposal)
    assert adapter.readback.await_args.kwargs["receipt"] == confirmed(p)
    assert row.api_calls_used == (
        accounting_adapter.NATIVE_PREFLIGHT_CALLS + 1 + accounting_adapter.NATIVE_VERIFY_CALLS
    )


async def test_a_refused_native_amendment_is_before_effect_and_keeps_the_restlet_reason(db, native_claimed):
    _, message, _ = native_claimed
    adapter = build_native(message)
    with patch(TRANSPORT, AsyncMock(return_value=refused("expected_before_mismatch"))):
        result, row = await _run(db, native_claimed, adapter)
    assert result["status"] == "rejected_before_effect" and row.result_json["code"] == "provider_rejected_without_save"
    assert state.permit_consumed(row) and "receipt" not in row.result_json
    assert adapter.refusal == "expected_before_mismatch" and adapter.readback.await_count == 0


@pytest.mark.parametrize(
    "answer",
    [
        TimeoutError("response lost after the save"),
        {"success": True, "record_type": "creditmemo", "record_id": "x", "work_key": "0" * 64, "financial_writes": 1},
        {"success": True, "status": "posted_pending_independent_verification", "financial_writes": "1"},
        {"success": False, "status": "outcome_unconfirmed", "financial_writes": None},
    ],
    ids=["transport_exception", "another_work_key", "financial_writes_not_an_int", "unconfirmed"],
)
async def test_an_answer_that_does_not_prove_this_work_is_unknown_until_the_readback_decides(
    db, native_claimed, answer
):
    _, message, _ = native_claimed
    p = message.structured_output["accounting_review"]
    transport = AsyncMock(side_effect=answer) if isinstance(answer, Exception) else AsyncMock(return_value=answer)
    adapter = build_native(message, readback=AsyncMock(return_value={"status": "needs_review", "reason": "unread"}))
    with patch(TRANSPORT, transport):
        result, row = await _run(db, native_claimed, adapter)
    assert result["status"] == "unknown" and state.permit_consumed(row)
    assert row.result_json["code"] in ("transport_indeterminate", "verification_unproven")
    assert "receipt" not in row.result_json and adapter.sent == "unknown"
    assert adapter.readback.await_count == 1
    if not isinstance(answer, Exception):
        assert adapter.readback.await_args.kwargs["receipt"] == answer
    assert p["record_id"] not in ("x",)


async def test_a_lost_native_answer_verified_by_the_readback_is_verified(db, native_claimed):
    _, message, _ = native_claimed
    adapter = build_native(message)
    with patch(TRANSPORT, AsyncMock(side_effect=TimeoutError("lost"))):
        result, row = await _run(db, native_claimed, adapter)
    assert result["status"] == "verified" and row.status == "verified"
    assert "receipt" not in row.result_json and adapter.sent == "unknown"


async def test_a_second_delivery_never_calls_the_restlet_again(db, native_claimed):
    _, message, _ = native_claimed
    p = message.structured_output["accounting_review"]
    with patch(TRANSPORT, AsyncMock(return_value=confirmed(p))) as transport:
        assert (await _run(db, native_claimed, build_native(message)))[0]["status"] == "verified"
        result, row = await _run(db, native_claimed, build_native(message))
    # The settled row answers the second delivery; the RESTlet is never asked again.
    assert transport.await_count == 1 and result["status"] == "verified"
    assert row.result_json["receipt"]["record_id"] == p["record_id"]


async def test_the_ledger_copy_of_a_native_readback_keeps_the_verdict_and_drops_the_record_blobs(db, native_claimed):
    _, message, _ = native_claimed
    p = message.structured_output["accounting_review"]
    verification = {
        "status": "verified",
        "record_type": p["record_type"],
        "record_id": p["record_id"],
        "credit_memo_id": p["record_id"],
        "invoice": {"lines": [{"n": i} for i in range(3000)]},
        "sales_order": {"lines": [{"n": i} for i in range(3000)]},
        "after": {"body": {"total": "440.00", "custbody_ecom_tx_ops_work_key": operation_identity(p)}},
        "source_revision": "rev-1",
        "ledger": [{"account": "AR", "debit": "0.00", "credit": "33.80"}],
        "related_records_unchanged": True,
        "retry_allowed": False,
        "financial_writes": 0,
        "scope": "native_amendment_and_related_postings",
        "full_reconciliation_required": True,
    }
    adapter = build_native(message, readback=AsyncMock(return_value=verification))
    with patch(TRANSPORT, AsyncMock(return_value=confirmed(p))):
        result, row = await _run(db, native_claimed, adapter)
    assert result["status"] == "verified"
    stored = row.result_json["verification"]
    assert stored["status"] == "verified" and stored["ledger"] == verification["ledger"]
    assert stored["record_id"] == p["record_id"] and stored["source_revision"] == "rev-1"
    assert not {"invoice", "sales_order", "after"} & set(stored)
    assert len(json.dumps(stored)) < 8_000
    # The card keeps the full readback the person reviews.
    assert adapter.verification["after"] == verification["after"]


async def test_the_native_tool_surface_never_sends_even_when_approved():
    """The native amendment leaves only through the kernel's adapter; the tool call that
    used to reach the durable dispatcher is refused outright, with nothing written."""
    from uuid import uuid4

    from app.services.chat.tools import execute_tool_call
    from app.services.transaction_ops.native_accounting_service import TOOL

    with patch(TRANSPORT, AsyncMock()) as transport:
        raw = await execute_tool_call(
            tool_name=TOOL,
            tool_input={"recordType": "creditmemo", "recordId": "1", "proposal_digest": "0" * 64},
            tenant_id=uuid4(),
            actor_id=uuid4(),
            db=None,
            correlation_id=None,
            human_approved=True,
            approval_context={"confirmation_id": str(uuid4())},
        )
    result = json.loads(raw)
    assert result["success"] is False and result["status"] == "not_submitted"
    assert result["financial_writes"] == 0 and result["retry_allowed"] is False
    transport.assert_not_awaited()
