"""G3.2a: a chat confirmation card is an approval source for the write kernel.

The same ledger row, claim rules and one-use permit the scheduled path has, bound to the
card instead of a proposal. Real database: the claim's uniqueness and the permit's
immutability are the database's, not the test's.
"""

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.chat import ChatMessage
from app.models.transaction_ops import TransactionOperation
from app.services.transaction_ops import chat_confirmation
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.accounting_recovery import evidence_digest
from app.services.transaction_ops.resolution_plan import operation_identity
from tests.test_accounting_recheck import approved_credit  # noqa: F401
from tests.test_accounting_recovery import interrupted_credit  # noqa: F401

FINGERPRINT = "c" * 64


@pytest.fixture
def authorized(monkeypatch):
    """The approver's humanity and the policy are checked by authorize_accounting_write on
    its own session; here it is a recorded call, and one test makes it refuse."""
    stub = AsyncMock()
    monkeypatch.setattr(chat_confirmation, "authorize_accounting_write", stub)
    return stub


async def _row(db, claimed):
    return await db.scalar(select(TransactionOperation).where(TransactionOperation.id == claimed.operation_id))


async def _bind_card(db, message, claimed):
    """What the orchestrator does after the claim: the card stores the ledger row id."""
    message.structured_output = {**message.structured_output, "operation_id": str(claimed.operation_id)}
    await db.flush()


async def _reserve(db, tenant_id, claimed, **kwargs):
    return await state.reserve_operation_dispatch(
        db,
        tenant_id,
        claimed,
        provider=chat_confirmation.PROVIDER_MCP,
        payload_fingerprint=FINGERPRINT,
        authorize=chat_confirmation.authorize_dispatch,
        **kwargs,
    )


async def test_a_confirmation_claims_one_ledger_row_with_its_business_identity(db, interrupted_credit, authorized):  # noqa: F811
    actor, _, _, message, _, _ = interrupted_credit
    so = message.structured_output
    p = so["accounting_review"]
    claimed = await chat_confirmation.claim(db, actor.tenant_id, message, actor_id=actor.id)
    row = await _row(db, claimed)
    assert row.status == "executing" and row.proposal_id is None
    assert row.approval_kind == "chat_confirmation" and row.approval_id == message.id
    assert row.surface == "chat" and row.provider == "netsuite_mcp"
    assert row.adapter == chat_confirmation.adapter_of(p)
    assert row.work_key == operation_identity(p) == claimed.work_key
    assert row.base_work_key == row.work_key and row.retry_of_operation_id is None
    assert row.result_json == {"evidence_digest": evidence_digest(so), "approved_by": str(actor.id)}
    assert claimed.approval_kind == "chat_confirmation" and claimed.approval_id == message.id
    assert claimed.action == (p.get("kind") or "invoice_tax") and claimed.before_json == {} == claimed.after_json
    authorized.assert_awaited_once()
    assert authorized.await_args.args[1:3] == (actor.tenant_id, actor.id)
    attempt = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.resource_id == str(row.id), AuditEvent.action == "transaction_ops.operation.attempt"
        )
    )
    assert attempt.payload["approval_id"] == str(message.id)


def test_each_treatment_names_its_adapter():
    from tests.test_accounting_approval_flow import kind_proposal

    expected = {
        "tax": "invoice_tax",
        "credit": "sales_credit",
        "discount": "invoice_discount",
        "sales_order": "sales_order_alignment",
        "native_credit": "native_amendment",
        "api_credit": "credit_api",
    }
    for kind, adapter in expected.items():
        assert chat_confirmation.adapter_of(kind_proposal(kind)) == adapter, kind


async def test_the_same_confirmation_and_the_same_work_are_claimed_once(db, interrupted_credit, authorized):  # noqa: F811
    actor, _, _, message, _, _ = interrupted_credit
    await chat_confirmation.claim(db, actor.tenant_id, message, actor_id=actor.id)
    with pytest.raises(state.StateError, match="approval_already_claimed"):
        await chat_confirmation.claim(db, actor.tenant_id, message, actor_id=actor.id)
    twin = ChatMessage(
        tenant_id=actor.tenant_id,
        session_id=message.session_id,
        role="assistant",
        content="",
        structured_output=dict(message.structured_output),
    )
    db.add(twin)
    await db.flush()
    with pytest.raises(state.StateError, match="operation_already_attempted"):
        await chat_confirmation.claim(db, actor.tenant_id, twin, actor_id=actor.id)


async def test_a_different_intent_on_the_same_document_waits_for_the_attempt_in_flight(
    db,
    interrupted_credit,  # noqa: F811
    authorized,
):
    actor, _, _, message, _, _ = interrupted_credit
    await chat_confirmation.claim(db, actor.tenant_id, message, actor_id=actor.id)
    so = message.structured_output
    p = {**so["accounting_review"], "proposed_fields": {**so["accounting_review"]["proposed_fields"], "memo": "x"}}
    other = ChatMessage(
        tenant_id=actor.tenant_id,
        session_id=message.session_id,
        role="assistant",
        content="",
        structured_output={**so, "accounting_review": p},
    )
    db.add(other)
    await db.flush()
    assert operation_identity(p) != operation_identity(so["accounting_review"])
    with pytest.raises(state.StateError, match="entity_in_flight"):
        await chat_confirmation.claim(db, actor.tenant_id, other, actor_id=actor.id)


@pytest.mark.parametrize("problem", ["token", "status", "actor", "tenant", "policy"])
async def test_a_claim_refuses_a_card_that_is_not_an_exact_executing_approval(
    db,
    interrupted_credit,  # noqa: F811
    authorized,
    tenant_b,
    admin_user_b,
    problem,  # noqa: F811
):
    actor, _, _, message, _, _ = interrupted_credit
    so = message.structured_output
    actor_id, tenant_id = actor.id, actor.tenant_id
    if problem == "token":
        message.structured_output = {**so, "tool_input": {**so["tool_input"], "data": "{}"}}
    elif problem == "status":
        message.structured_output = {**so, "status": "pending"}
    elif problem == "actor":
        actor_id = admin_user_b[0].id
    elif problem == "tenant":
        tenant_id = tenant_b.id
    elif problem == "policy":
        authorized.side_effect = ValueError("Current policy blocks this correction. No update was sent.")
    with pytest.raises(ValueError) as exc:
        await chat_confirmation.claim(db, tenant_id, message, actor_id=actor_id)
    assert {
        "token": "confirmation_token_invalid",
        "status": "confirmation_not_executing",
        "actor": "approver_not_session_owner",
        "tenant": "approver_not_session_owner",
        "policy": "Current policy blocks",
    }[problem] in str(exc.value)
    assert (
        await db.scalar(select(TransactionOperation.id).where(TransactionOperation.approval_id == message.id)) is None
    )


async def test_the_permit_is_one_use_and_bound_to_the_card_as_stored(db, interrupted_credit, authorized):  # noqa: F811
    actor, _, _, message, _, _ = interrupted_credit
    claimed = await chat_confirmation.claim(db, actor.tenant_id, message, actor_id=actor.id)
    with pytest.raises(state.StateError, match="confirmation_changed"):
        await _reserve(db, actor.tenant_id, claimed)  # the card does not carry the row yet
    await _bind_card(db, message, claimed)
    assert await _reserve(db, actor.tenant_id, claimed) is True
    row = await _row(db, claimed)
    assert state.permit_consumed(row) and row.api_calls_used == 1
    assert row.result_json["evidence_digest"] == evidence_digest(message.structured_output)
    assert await _reserve(db, actor.tenant_id, claimed) is False
    assert authorized.await_count == 2  # once at the claim, once at the permit; never for the refused duplicate


@pytest.mark.parametrize("problem", ["resolved", "edited", "token", "provider", "policy", "no_source"])
async def test_the_permit_refuses_a_card_that_changed_since_the_claim(db, interrupted_credit, authorized, problem):  # noqa: F811
    actor, _, _, message, _, _ = interrupted_credit
    claimed = await chat_confirmation.claim(db, actor.tenant_id, message, actor_id=actor.id)
    await _bind_card(db, message, claimed)
    so = message.structured_output
    kwargs = {}
    if problem == "resolved":
        message.structured_output = {**so, "status": "failed"}
    elif problem == "edited":
        message.structured_output = {**so, "accounting_review": {**so["accounting_review"], "record_id": "999"}}
    elif problem == "token":
        message.structured_output = {**so, "confirmation_token": "forged"}
    elif problem == "provider":
        kwargs = {"provider": chat_confirmation.PROVIDER_NATIVE}
    elif problem == "policy":
        authorized.side_effect = ValueError("policy changed")
    elif problem == "no_source":
        kwargs = {"authorize": None}
    await db.flush()
    with pytest.raises(state.StateError) as exc:
        await state.reserve_operation_dispatch(
            db,
            actor.tenant_id,
            claimed,
            **{
                "provider": chat_confirmation.PROVIDER_MCP,
                "payload_fingerprint": FINGERPRINT,
                "authorize": chat_confirmation.authorize_dispatch,
                **kwargs,
            },
        )
    assert (
        exc.value.code
        == {
            "resolved": "confirmation_changed",
            "edited": "confirmation_changed",
            "token": "confirmation_changed",
            "provider": "unsupported_dispatch_provider",
            "policy": "approval_not_authorized",
            "no_source": "approval_source_required",
        }[problem]
    )
    row = await _row(db, claimed)
    assert row.status == "executing" and not state.permit_consumed(row)


async def test_a_reconstructed_claim_cannot_use_another_rows_permit(db, interrupted_credit, authorized):  # noqa: F811
    actor, _, _, message, _, _ = interrupted_credit
    claimed = await chat_confirmation.claim(db, actor.tenant_id, message, actor_id=actor.id)
    await _bind_card(db, message, claimed)
    forged = claimed.model_copy(update={"approval_id": uuid.uuid4()})
    with pytest.raises(state.StateError, match="claimed_operation_mismatch"):
        await _reserve(db, actor.tenant_id, forged)
    assert not state.permit_consumed(await _row(db, claimed))
