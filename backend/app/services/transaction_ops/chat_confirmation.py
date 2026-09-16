"""The chat confirmation card as an approval source for the write kernel.

A ``WriteConfirmationCard`` carrying an ``accounting_review`` is claimed on the operation
ledger exactly the way a transaction proposal is (docs/superpowers/specs/
2026-09-15-write-kernel-design.md, section 4): one executing row per approval, one attempt
per piece of work, one in-flight attempt per document. The approval's validity is the
card's: the HMAC token still verifies, the card has been CAS-accepted to ``executing``, the
approver owns the session and is still a permitted human, and the policy still allows the
tool. The permit re-checks all of it against the card as stored, so a card edited or
resolved after the claim can never be sent.

The card is the payload of record. The ledger binds it by ``evidence_digest`` (the same
canonical digest ``accounting_recovery`` uses) rather than copying it, so amounts stay the
Decimal strings the card holds and JSON numbers in native evidence never enter the ledger.
"""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select

from app.models.chat import ChatMessage, ChatSession
from app.services.chat.write_confirmation_service import validate_and_extract_confirmation
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.accounting_group import authorize_accounting_write
from app.services.transaction_ops.accounting_recovery import evidence_digest
from app.services.transaction_ops.native_accounting_service import TOOL as NATIVE_TOOL
from app.services.transaction_ops.resolution_plan import operation_identity
from app.services.transaction_ops.treatments import collision_key, is_mcp, treatment_of

KIND = "chat_confirmation"
PROVIDER_MCP = "netsuite_mcp"
PROVIDER_NATIVE = "netsuite_native"
# The treatment's verification contract names the adapter that carries it, except that the
# transport is a field on the proposal, never inferred from the kind (PR #262's group crash).
ADAPTERS_BY_VERIFICATION = {
    "invoice": "invoice_tax",
    "discount": "invoice_discount",
    "credit": "sales_credit",
    "order": "sales_order_alignment",
    "amendment": "native_amendment",
}
ADAPTER_CREDIT_API = "credit_api"


def adapter_of(proposal) -> str:
    if is_mcp(proposal):
        return ADAPTER_CREDIT_API
    return ADAPTERS_BY_VERIFICATION[treatment_of(proposal).verification]


def provider_of(so) -> str:
    return PROVIDER_NATIVE if so.get("tool_name") == NATIVE_TOOL else PROVIDER_MCP


def intent_of(tenant_id, message, so, *, actor_id, now) -> state.ApprovedIntent:
    """The ledger's view of an accounting card: identity, scope, provider, adapter."""
    p = so.get("accounting_review") or {}
    if not p:
        raise state.StateError("accounting_review_required")
    if p.get("tenant_id") != str(tenant_id):
        raise state.StateError("accounting_operation_tenant_mismatch")
    treatment = treatment_of(p)
    record_type, document = collision_key(p)
    scope = p["scope"]
    account = str(scope["netsuite_account_id"]).replace("_", "-").lower()
    subsidiary = str(scope.get("subsidiary_id") or "")
    return state.ApprovedIntent(
        approval_kind=KIND,
        approval_id=message.id,
        approved_by=actor_id,
        surface="group" if so.get("accounting_group_child") else "chat",
        provider=provider_of(so),
        adapter=adapter_of(p),
        action=treatment.kind,
        work_key=operation_identity(p),
        entity_key=state.business_digest(
            {"account": account, "subsidiary": subsidiary, "record_type": record_type, "document": document}
        ),
        netsuite_account_id=str(scope["netsuite_account_id"]),
        subsidiary_id=subsidiary,
        record_type=p["record_type"],
        target_record_id=str(p["record_id"]) if p.get("record_id") is not None else None,
        evidence_digest=evidence_digest(so),
        valid_until=now + state._OPERATION_TIME,
        config_id=_uuid_or_none(p.get("config_id")),
    )


def _uuid_or_none(value):
    """A config id is informational on a chat claim; a card without one (or with a
    non-UUID one from an older builder) still claims."""
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


CLAIM_REFUSALS = {
    "approval_already_claimed": "This confirmation already has an execution record. No duplicate update was sent.",
    "operation_already_attempted": "This correction already has an execution record. No duplicate update was sent. "
    "Review the recorded verification or reconciliation result before taking another action.",
    "entity_in_flight": "Another approved correction is checking this document. No additional update was sent.",
    "confirmation_token_invalid": "Confirmation token is invalid or tampered.",
    "confirmation_not_executing": "This confirmation is already being processed by another request.",
    "approver_not_session_owner": "Only the session owner can send this correction. No update was sent.",
    "retry_requires_rejected_before_effect": "Only a correction that was refused before any effect can be retried.",
}


def refusal_text(exc) -> str:
    """The sentence a person sees for a claim refusal: a ledger code's text, or the
    authorization's own message (it is written for people)."""
    code = getattr(exc, "code", None)
    return CLAIM_REFUSALS.get(code, str(exc))


async def _session_owner(db, tenant_id, message):
    return await db.scalar(
        select(ChatSession.user_id).where(ChatSession.id == message.session_id, ChatSession.tenant_id == tenant_id)
    )


async def claim(db, tenant_id, message, *, actor_id, now=None):
    """Claim an approved accounting card on the ledger. Called after the card's CAS moved it
    to ``executing`` and before any provider read. Refusals are StateErrors with a code, or
    the authorization's own ValueError (its text is what the person is shown)."""
    now = now or datetime.now(timezone.utc)
    so = message.structured_output or {}
    valid, tool_name, tool_input = validate_and_extract_confirmation(so, str(message.session_id))
    if not valid:
        raise state.StateError("confirmation_token_invalid")
    if so.get("status") != "executing":
        raise state.StateError("confirmation_not_executing")
    if message.tenant_id != tenant_id or await _session_owner(db, tenant_id, message) != actor_id:
        raise state.StateError("approver_not_session_owner", 403)
    await authorize_accounting_write(db, tenant_id, actor_id, tool_name, tool_input)
    intent = intent_of(tenant_id, message, so, actor_id=actor_id, now=now)
    return await state.claim_intent(db, tenant_id, intent, now=now)


async def authorize_dispatch(db, tenant_id, operation, claimed, now):
    """The permit-time check for a chat claim (state_service.reserve_operation_dispatch's
    ``authorize``): the card still carries this claim unchanged, and its approver is still
    a permitted human the policy allows. Raises StateError; nothing here sends."""
    message = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.tenant_id == tenant_id, ChatMessage.id == operation.approval_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if message is None:
        raise state.StateError("confirmation_missing")
    so = message.structured_output or {}
    valid, tool_name, tool_input = validate_and_extract_confirmation(so, str(message.session_id))
    recorded = operation.result_json or {}
    if (
        not valid
        or so.get("status") != "executing"
        or so.get("operation_id") != str(operation.id)
        or evidence_digest(so) != recorded.get("evidence_digest")
        or operation_identity(so.get("accounting_review")) != operation.work_key
    ):
        raise state.StateError("confirmation_changed")
    approver = UUID(str(recorded.get("approved_by")))
    if await _session_owner(db, tenant_id, message) != approver:
        raise state.StateError("approver_not_session_owner", 403)
    try:
        await authorize_accounting_write(db, tenant_id, approver, tool_name, tool_input)
    except ValueError as exc:
        # The ledger records a code, never the authorization's message.
        raise state.StateError("approval_not_authorized", 403) from exc
