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
