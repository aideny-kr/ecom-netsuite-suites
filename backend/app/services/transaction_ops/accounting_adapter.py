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
from datetime import datetime, timedelta, timezone
from functools import cached_property
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
# The native amendment's preflight is the native service's whole fan-out (case scope,
# binding, fresh source/evidence/support, the rebuilt intent, then the RESTlet's
# capabilities and preview reads); its readback re-reads the evidence plus one snapshot.
NATIVE_PREFLIGHT_CALLS = 12
NATIVE_VERIFY_CALLS = 10
NATIVE_APPROVAL_WINDOW = timedelta(minutes=2)  # how long the RESTlet may honour one permit
# The ledger row is bounded (state_service._bounded_json caps it at 64 KiB). The verdict
# keys below are scalars; the GL breakdown grows with the record, so the projection keeps
# its shape and digest past this budget rather than its rows. The card keeps all of it.
NATIVE_LEDGER_BUDGET = 32_768
# The native readback returns whole records for the person (the after snapshot, the
# invoice and sales order it compared against); the ledger keeps the verdict and the
# ledger rows, never the record blobs (state_service._bounded_json caps a row at 64 KiB).
NATIVE_LEDGER_VERIFICATION_KEYS = (
    "status",
    "reason",
    "record_type",
    "record_id",
    "credit_memo_id",
    "source_revision",
    "ledger",
    "related_records_unchanged",
    "retry_allowed",
    "financial_writes",
    "scope",
    "full_reconciliation_required",
)
_CODE = re.compile(r"[a-z][a-z0-9_:.]{2,79}")
# The provider answer keys that identify a saved record; the card keeps the same set
# (orchestrator) so a card never displays a different receipt than its ledger row.
RECEIPT_IDS = ("recordId", "id", "internalId", "record_id", "record_type", "work_key")
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
    readback: Callable  # (db, tenant_id, proposal, receipt=...) -> {"status": ...}
    dispatch: Callable | None = None  # execute_tool_call(**kwargs) -> str; the MCP card's send, unused natively
    approval_context: dict | None = None
    provider: str = chat_confirmation.PROVIDER_MCP
    refusal: str | None = None
    receipt: dict | None = None  # what the provider answered, as parsed
    sent: str | None = None  # how send classified it: accepted | failed | unknown
    verification: dict | None = None
    preflight_calls: int = PREFLIGHT_CALLS
    verify_calls: int = VERIFY_CALLS
    USES_TOOL_DISPATCHER = True  # the signed tool dispatcher is this adapter's send

    @property
    def proposal(self) -> dict:
        return self.message.structured_output["accounting_review"]

    async def preflight(self, db, tenant_id, claimed, *, read):
        try:
            await read(self.preflight_calls, self.validate, self.tool_name, self.tool_input, self.proposal)
        except ExecutionStoppedError:
            raise
        except ValueError as exc:
            self.refusal = str(exc)
            raise PreconditionChangedError(refusal_code(exc)) from exc
        except Exception as exc:
            # Not a changed precondition: a read that could not complete. The ledger records
            # the generic code; the card says what actually happened, as the old path did.
            self.refusal = f"the approved evidence could not be revalidated ({type(exc).__name__}: {exc})"[:300]
            raise
        return {}

    def wire_fingerprint(self) -> str:
        """What the permit is bound to: the signed tool input the dispatcher sends."""
        return digest(self.tool_input)

    async def permit(self, db, tenant_id, claimed) -> dict | None:
        """The one-use permit, or the receipt an earlier delivery already recorded.

        Returns None when this delivery holds the permit and may send exactly once;
        otherwise the ledger's answer for a replayed delivery (nothing may be sent)."""
        try:
            granted = await state.reserve_operation_dispatch(
                db,
                tenant_id,
                claimed,
                provider=self.provider,
                payload_fingerprint=self.wire_fingerprint(),
                authorize=chat_confirmation.authorize_dispatch,
            )
        except state.StateError as exc:
            # Refused before the permit existed: nothing was sent, and the code says why.
            self.refusal = REFUSALS.get(exc.code, "The approval no longer holds. No update was sent.")
            raise PreconditionChangedError(exc.code) from exc
        if granted:
            return None
        # An earlier delivery consumed the permit; whatever it recorded is this
        # delivery's receipt too, so the card and the audit do not lose it.
        row = await state._one(db, tenant_id, TransactionOperation, claimed.operation_id)
        self.receipt = (row.result_json or {}).get("receipt")
        self.sent = "accepted" if self.receipt else "unknown"
        return {"status": "unknown", "code": "dispatch_already_reserved", "verified": False}

    async def send(self, db, tenant_id, claimed, preflight) -> dict:
        """The permit, then exactly one delivery; the delivery is the adapter's."""
        replayed = await self.permit(db, tenant_id, claimed)
        if replayed is not None:
            return replayed
        return await self.deliver(db, tenant_id, claimed)

    async def deliver(self, db, tenant_id, claimed) -> dict:
        if self.dispatch is None:
            raise RuntimeError("dispatcher_required")
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
        ids = self._identity(result)
        outcome = classify_write_outcome(result)
        if outcome == "indeterminate":
            return self._sent(
                {"status": "unknown", "code": "transport_indeterminate", "verified": False, "identity": ids}
            )
        if outcome == "failed":
            self.refusal = self._refusal_text(result, "NetSuite reported the write failed.")
            if ids:
                # An error beside a record identity is not proof of no effect: only the
                # readback may say whether the record was saved.
                return self._sent(
                    {
                        "status": "unknown",
                        "code": "provider_rejected_with_identity",
                        "verified": False,
                        "identity": ids,
                    }
                )
            return self._sent({"status": "failed", "code": "provider_rejected", "verified": False})
        return self._sent({"status": "accepted", "verified": False, **ids})

    @staticmethod
    def _identity(result) -> dict:
        """What the provider named, if anything: kept on the row when it is not a receipt."""
        if not isinstance(result, dict):
            return {}
        return {key: str(result[key]) for key in RECEIPT_IDS if result.get(key) is not None}

    def _refusal_text(self, result, default: str) -> str:
        return _extract_error_message(result) or (result.get("reason") if isinstance(result, dict) else None) or default

    def _sent(self, receipt: dict) -> dict:
        self.sent = receipt["status"]
        return receipt

    async def verify(self, db, tenant_id, claimed, preflight, *, read):
        verification = await read(self.verify_calls, self.readback, self.proposal, receipt=self.receipt)
        self.verification = json_copy(verification)
        if self.verification.get("status") != "verified":
            return None
        return ledger_safe(self.ledger_verification(self.verification))

    @classmethod
    def ledger_verification(cls, verification: dict) -> dict:
        """The part of a readback the ledger row keeps; the card keeps all of it. A class
        method so the recovery scan, which reads back without an adapter instance, stores
        the same projection (``ledger_view``)."""
        return verification


@dataclass
class NativeAmendmentAdapter(AccountingCardAdapter):
    """The native amendment card (RESTlet ``customscript_ecom_acct_amend``) behind the kernel.

    Differences from the MCP card, each one a fact of the native path: the ledger row's
    provider is ``netsuite_native``; the native service's FULL preflight (fresh evidence,
    the rebuilt intent, the RESTlet's capabilities and preview) runs BEFORE the permit,
    where the retired durable dispatcher used to run it after minting a permit of its own;
    the send is the transport's ``apply`` call itself, made once behind the ledger's permit
    with the ledger row as the RESTlet's ``approval_audit_id`` (NetSuite logs it in its own
    audit) and the business identity as ``work_key`` (the RESTlet stamps it on the record
    and the readback compares it, so a lineage retry still sends the base key); and an
    answer counts as a receipt only when it proves THIS work (record, work key, one
    financial write), a clean ``not_submitted`` with zero writes is the RESTlet's refusal,
    and anything else, including a lost response, is unknown until the readback decides.
    """

    provider: str = chat_confirmation.PROVIDER_NATIVE
    preflight_calls: int = NATIVE_PREFLIGHT_CALLS
    verify_calls: int = NATIVE_VERIFY_CALLS
    USES_TOOL_DISPATCHER = False  # the RESTlet is the send; no tool dispatcher is involved

    @cached_property
    def wire(self) -> dict:
        """What leaves for the RESTlet, minus the per-send approval fields: the permit's
        fingerprint and the send itself read the proposal through this one accessor."""
        p = self.proposal
        return {
            "request": p["native_request"],
            "expected_before": p["native_preview"]["beforeSnapshot"],
            "work_key": self.work_key,
        }

    def wire_fingerprint(self) -> str:
        return digest(self.wire)

    @cached_property
    def work_key(self) -> str:
        from app.services.transaction_ops.resolution_plan import operation_identity

        return operation_identity(self.proposal)

    async def deliver(self, db, tenant_id, claimed) -> dict:
        from app.services.transaction_ops import native_accounting_transport as transport

        p, key = self.proposal, self.work_key
        try:
            result = await transport._request(
                db,
                tenant_id,
                p["connection_id"],
                p["scope"]["netsuite_account_id"],
                "apply",
                {
                    **self.wire,
                    "approval_audit_id": str(claimed.operation_id),
                    "approval_expires_at": (datetime.now(timezone.utc) + NATIVE_APPROVAL_WINDOW).isoformat(),
                },
            )
        except Exception as exc:
            # The request may have reached NetSuite: only the readback can say.
            self.receipt = {"unconfirmed": True, "reason": f"{type(exc).__name__}: {exc}"[:300]}
            return self._sent({"status": "unknown", "code": "transport_indeterminate", "verified": False})
        self.receipt = result if isinstance(result, dict) else {"value": result}
        if not isinstance(result, dict):
            return self._sent({"status": "unknown", "code": "transport_indeterminate", "verified": False})
        writes = result.get("financial_writes")
        confirmed = (
            result.get("success") is True
            and result.get("record_type") == p["record_type"]
            and result.get("record_id") == p["record_id"]
            and result.get("work_key") == key
            and type(writes) is int
            and writes == 1
        )
        refused = (
            result.get("success") is False
            and result.get("status") == "not_submitted"
            and type(writes) is int
            and writes == 0
        )
        if confirmed:
            return self._sent(
                {
                    "status": "accepted",
                    "verified": False,
                    "record_type": str(p["record_type"]),
                    "record_id": str(p["record_id"]),
                    "work_key": key,
                }
            )
        if refused:
            self.refusal = self._refusal_text(result, "NetSuite did not apply the amendment.")
            return self._sent({"status": "failed", "code": "provider_rejected", "verified": False})
        return self._sent(
            {
                "status": "unknown",
                "code": "transport_indeterminate",
                "verified": False,
                "identity": self._identity(result),
            }
        )

    @classmethod
    def ledger_verification(cls, verification: dict) -> dict:
        kept = {key: verification[key] for key in NATIVE_LEDGER_VERIFICATION_KEYS if key in verification}
        if len(json.dumps(kept, default=str)) <= NATIVE_LEDGER_BUDGET:
            return kept
        # A balanced GL is not bounded by row count: a record with hundreds of lines would
        # push a legitimately verified readback past the row's own limit and leave the
        # amendment uncompletable. The row keeps the breakdown's shape, not its rows.
        gl = kept.get("ledger")
        rows = gl.get("rows") if isinstance(gl, dict) else gl
        kept["ledger"] = {
            "rows": len(rows or []),
            "digest": digest(gl),
            "omitted": "gl_exceeds_ledger_budget",
        }
        return kept


# One adapter per provider the chat confirmation source records on the row; the
# orchestrator picks by provider and the recovery scan projects readbacks the same way.
ADAPTERS_BY_PROVIDER = {
    chat_confirmation.PROVIDER_MCP: AccountingCardAdapter,
    chat_confirmation.PROVIDER_NATIVE: NativeAmendmentAdapter,
}


def ledger_view(provider: str, verification: dict) -> dict:
    """The readback as the ledger row stores it for this provider (exact strings, no
    record blobs where the adapter drops them), for readbacks made without an adapter."""
    return ledger_safe(ADAPTERS_BY_PROVIDER.get(provider, AccountingCardAdapter).ledger_verification(verification))
