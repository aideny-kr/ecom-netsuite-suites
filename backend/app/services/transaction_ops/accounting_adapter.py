"""The accounting card's write adapter: one treatment's preflight, send and readback
behind the write kernel (docs/superpowers/specs/2026-09-15-write-kernel-design.md, §5).

``preflight`` is the treatment's own revalidation (today's ``tax_correction.validate_approved``
fan-out; every ValueError it raises is a changed precondition), ``send`` is the signed
confirmation dispatcher behind the one-use permit, ``verify`` is the treatment's
``verify_after``. The adapter writes no ledger row and mints no permit: the permit comes
from state_service.reserve_operation_dispatch with the chat confirmation's own authorizer,
and the kernel records every outcome.

The card is rendered from what the adapter saw (``refusal``, ``receipt``, ``verification``)
plus the ledger row; the ledger itself records codes and id-only receipts, never provider
prose, and never a JSON float (the readback copy stored on the row is stringified).
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from app.models.transaction_ops import TransactionOperation
from app.services.chat.tool_call_results import _extract_error_message
from app.services.chat.write_outcome import classify_write_outcome
from app.services.transaction_ops import chat_confirmation
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.accounting_group import digest
from app.services.transaction_ops.write_kernel import ExecutionStoppedError, PreconditionChangedError

PREFLIGHT_CALLS = 8  # what a treatment's revalidation costs the operation budget
VERIFY_CALLS = 8
_CODE = re.compile(r"[a-z][a-z0-9_:.]{2,79}")
_RECEIPT_IDS = ("recordId", "id", "internalId", "record_id", "record_type", "work_key")
REFUSALS = {
    "confirmation_changed": "The approval card changed after it was accepted. No update was sent.",
    "approval_not_authorized": "The approver is no longer permitted to send this correction. No update was sent.",
    "approver_not_session_owner": "Only the session owner can send this correction. No update was sent.",
    "dispatch_disabled": "Sending is switched off by the operator. No update was sent.",
    "operation_budget_exhausted": "The correction ran out of its read budget before sending. No update was sent.",
}


def refusal_code(exc) -> str:
    """A treatment's snake_case reason is the ledger code; a sentence for a person is not."""
    text = str(exc)
    return text if _CODE.fullmatch(text) else "evidence_revalidation_failed"


def json_copy(value):
    """A plain-JSON copy: Decimals and other exact types become strings, NaN is refused."""
    return json.loads(json.dumps(value, default=str, allow_nan=False))


def ledger_safe(value):
    """The readback as the ledger may store it: exact strings, never binary floats."""

    def visit(item):
        if isinstance(item, float):
            return str(item)
        if isinstance(item, dict):
            return {str(k): visit(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [visit(v) for v in item]
        return item

    return visit(json_copy(value))


@dataclass
class AccountingCardAdapter:
    name: str
    message: Any
    tool_name: str
    tool_input: dict
    actor_id: Any
    session_id: str
    correlation_id: str
    validate: Callable  # (db, tenant_id, tool_name, tool_input, proposal) -> None; ValueError = changed
    dispatch: Callable  # execute_tool_call(**kwargs) -> str
    readback: Callable  # (db, tenant_id, proposal, receipt=...) -> {"status": ...}
    approval_context: dict | None = None
    provider: str = chat_confirmation.PROVIDER_MCP
    refusal: str | None = None
    receipt: dict | None = None  # what the provider answered, as parsed
    sent: str | None = None  # how send classified it: accepted | failed | unknown
    verification: dict | None = None

    @property
    def proposal(self) -> dict:
        return self.message.structured_output["accounting_review"]

    async def preflight(self, db, tenant_id, claimed, *, read):
        try:
            await read(PREFLIGHT_CALLS, self.validate, self.tool_name, self.tool_input, self.proposal)
        except ExecutionStoppedError:
            raise
        except ValueError as exc:
            self.refusal = str(exc)
            raise PreconditionChangedError(refusal_code(exc)) from exc
        return {}

    async def send(self, db, tenant_id, claimed, preflight) -> dict:
        try:
            granted = await state.reserve_operation_dispatch(
                db,
                tenant_id,
                claimed,
                provider=self.provider,
                payload_fingerprint=digest(self.tool_input),
                authorize=chat_confirmation.authorize_dispatch,
            )
        except state.StateError as exc:
            # Refused before the permit existed: nothing was sent, and the code says why.
            self.refusal = REFUSALS.get(exc.code, "The approval no longer holds. No update was sent.")
            raise PreconditionChangedError(exc.code) from exc
        if not granted:
            # An earlier delivery consumed the permit; whatever it recorded is this
            # delivery's receipt too, so the card and the audit do not lose it.
            row = await state._one(db, tenant_id, TransactionOperation, claimed.operation_id)
            self.receipt = (row.result_json or {}).get("receipt")
            self.sent = "accepted" if self.receipt else "unknown"
            return {"status": "unknown", "code": "dispatch_already_reserved", "verified": False}
        raw = await self.dispatch(
            human_approved=True,
            approval_context=self.approval_context,
            tool_name=self.tool_name,
            tool_input=self.tool_input,
            tenant_id=tenant_id,
            actor_id=self.actor_id,
            correlation_id=self.correlation_id,
            db=db,
            session_id=self.session_id,
        )
        try:
            result = json.loads(raw)
        except (TypeError, ValueError):
            self.receipt = {"unreadable": True}
            return self._sent({"status": "unknown", "code": "receipt_unreadable", "verified": False})
        self.receipt = result if isinstance(result, dict) else {"value": result}
        outcome = classify_write_outcome(result)
        if outcome == "indeterminate":
            return self._sent({"status": "unknown", "code": "transport_indeterminate", "verified": False})
        ids = {key: str(result[key]) for key in _RECEIPT_IDS if result.get(key) is not None}
        if outcome == "failed":
            self.refusal = _extract_error_message(result) or "NetSuite reported the write failed."
            if ids:
                # An error beside a record identity is not proof of no effect: only the
                # readback may say whether the record was saved.
                return self._sent({"status": "unknown", "code": "provider_rejected_with_identity", "verified": False})
            return self._sent({"status": "failed", "code": "provider_rejected", "verified": False})
        return self._sent({"status": "accepted", "verified": False, **ids})

    def _sent(self, receipt: dict) -> dict:
        self.sent = receipt["status"]
        return receipt

    async def verify(self, db, tenant_id, claimed, preflight, *, read):
        verification = await read(VERIFY_CALLS, self.readback, self.proposal, receipt=self.receipt)
        self.verification = json_copy(verification)
        if self.verification.get("status") != "verified":
            return None
        return ledger_safe(self.verification)
