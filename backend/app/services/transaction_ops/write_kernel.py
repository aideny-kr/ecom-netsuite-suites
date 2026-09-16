"""The write kernel: one loop from a claimed, approved intent to a ledger outcome.

    claim → preflight → send (exactly once, behind the one-use permit) → verify

Every exit is a row in the operation ledger with one of state_service.OUTCOMES
(docs/superpowers/specs/2026-09-15-write-kernel-design.md, sections 3 and 6):

* ``rejected_before_effect`` when the approved evidence no longer holds, the provider
  refused atomically, or budget ran out before any permit existed;
* ``committed_unverified`` when the provider identified the record as saved but the
  independent readback has not yet proved the approved end state;
* ``unknown`` when a permit was consumed and there is no trustworthy receipt;
* ``verified`` when the readback proved it; ``needs_review`` when only a person can decide.

Adapters (write_adapters.py) own the provider-specific reads, the wire payload and the one
send. They never write the ledger and cannot mint a permit: the permit comes from
state_service.reserve_operation_dispatch, called inside their ``send``, and the guard
trigger refuses a receipt on a row that never consumed one.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Protocol

from app.models.transaction_ops import TransactionOperation
from app.services.transaction_ops import state_service as state

READ_TIMEOUT_SECONDS = 120


class PreconditionChangedError(ValueError):
    """The approved evidence no longer holds. Raised by an adapter's preflight, before any
    permit exists, so the kernel ends the attempt ``rejected_before_effect`` with nothing sent."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ExecutionStoppedError(ValueError):
    """A read found a reason to stop before any send.

    ``keep_code`` marks a documented stop whose code is the ledger code (the source order's
    payment failed, an unsupported action); anything else is recorded under the generic
    revalidation code so an internal message never becomes ledger evidence.
    """

    def __init__(self, code: str, *, keep_code: bool = False):
        super().__init__(code)
        self.code = code
        self.keep_code = keep_code


class WriteAdapter(Protocol):
    name: str
    provider: str

    async def preflight(self, db, tenant_id, claimed, *, read) -> Any:
        """Fresh, budgeted reads that re-establish the approved evidence. Raises
        PreconditionChangedError when it no longer holds. Never sends, never reserves."""

    async def send(self, db, tenant_id, claimed, preflight) -> dict:
        """Exactly one provider call, after reserving the one-use permit. Returns the
        receipt: ``{"status": "accepted" | "failed" | "unknown", ...}``."""

    async def verify(self, db, tenant_id, claimed, preflight, *, read) -> dict | None:
        """Independent, budgeted readback. Returns the proof of the approved end state,
        or None when it cannot be established (contradicted or unavailable)."""


def result_of(row) -> dict:
    return {
        "operation_id": str(row.id),
        "status": row.status,
        "termination_reason": (row.result_json or {}).get("termination_reason", "stall"),
    }


def _recorded(row) -> bool:
    """Whether the ledger already holds an outcome a failure must not overwrite: a terminal
    row, or an open one that budget exhaustion, a recovery pass or an earlier delivery
    completed (only those write ``completed_at``). A proof may still move such a row
    forward; an exception may not move it back or replace its recorded reason."""
    return row.status not in state.OPEN or row.completed_at is not None


def _outcome_after(row, exc) -> tuple[str, str]:
    """The outcome and code for an attempt an exception ended, decided by the ledger row.

    After a receipt the attempt stays ``committed_unverified`` whatever the readback found;
    after a consumed permit it is ``unknown``; before either it is ``rejected_before_effect``.
    A documented stop (``ExecutionStoppedError(keep_code=True)``) and a changed precondition
    keep their code; any other exception is recorded under a generic code so an internal
    message never becomes ledger evidence.
    """
    sent = state.permit_consumed(row)  # a receipt is only ever recorded behind the permit
    if isinstance(exc, PreconditionChangedError) or (isinstance(exc, ExecutionStoppedError) and exc.keep_code):
        code = exc.code
    else:
        code = "verification_unavailable" if sent else "evidence_revalidation_failed"
    if row.status == "committed_unverified":
        return "committed_unverified", code
    return ("unknown" if sent else "rejected_before_effect"), code


async def execute(db, tenant_id, claimed, adapter: WriteAdapter, *, clock=None) -> dict:
    """Run a claimed operation through its adapter and record the outcome.

    The claim (state_service.claim_approved_operation) has already committed the executing
    row. A duplicate delivery of the same claim reads the durable outcome; it never obtains
    another permit, because the permit and the receipt are one-use on the row itself.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))

    async def read(cost, function, *args, **kwargs):
        permit = await state.reserve_operation_budget(db, tenant_id, claimed.operation_id, api_calls=cost, now=clock())
        if permit is None:
            raise ExecutionStoppedError("operation_budget_exhausted")
        seconds = (permit.deadline_at - clock()).total_seconds()
        if seconds <= 0:
            raise ExecutionStoppedError("operation_budget_exhausted")
        async with asyncio.timeout(min(seconds, READ_TIMEOUT_SECONDS)):
            return await function(db, tenant_id, *args, **kwargs)

    async def complete(outcome, code, **details):
        try:
            row = await state.complete_operation(
                db, tenant_id, claimed.operation_id, outcome=outcome, result_json={"code": code, **details}, now=clock()
            )
        except state.StateError as exc:
            if exc.code != "operation_terminal":
                raise
            # A duplicate delivery of a settled claim reads the durable outcome.
            row = await state._one(db, tenant_id, TransactionOperation, claimed.operation_id)
        return result_of(row)

    async def attempt():
        """The attempt itself: what the adapter found, sent and read back, as a decision
        (outcome, code, details). Recording the decision is not part of it."""
        preflight = await adapter.preflight(db, tenant_id, claimed, read=read)
        receipt = await adapter.send(db, tenant_id, claimed, preflight)
        if receipt["status"] == "failed":
            return "rejected_before_effect", "provider_rejected_without_save", {}
        if receipt["status"] == "accepted":
            # Saved by the provider's own account, not yet proven by an independent read:
            # the row says so before the readback, in case nothing after this runs.
            try:
                await state.record_receipt(db, tenant_id, claimed.operation_id, receipt, now=clock())
            except state.StateError as exc:
                if exc.code != "receipt_without_permit":
                    raise
                # The adapter reported a save without consuming the permit. That is an
                # adapter defect a person must look at; the kernel will not guess.
                return "needs_review", "adapter_receipt_without_permit", {}
        proof = await adapter.verify(db, tenant_id, claimed, preflight, read=read)
        if proof is not None:
            return "verified", "independently_verified", {"verification": proof}
        # A receipt without proof stays committed_unverified (readback again later); no
        # receipt at all is unknown (existence must be reconciled first). Never a resend.
        return ("committed_unverified" if receipt["status"] == "accepted" else "unknown"), "verification_unproven", {}

    try:
        outcome, code, details = await attempt()
    except Exception as exc:
        # An exception may follow an actual send (a permit reserved, a receipt recorded);
        # only the committed ledger row decides what the failure is. Preflight must never
        # reserve, so a changed precondition after a permit is treated as possibly sent.
        try:
            row = await state._one(db, tenant_id, TransactionOperation, claimed.operation_id)
        except Exception as ledger_exc:
            # The ledger cannot be read, so nothing can be recorded and nothing is guessed:
            # the row stays executing and recover_expired_operation settles it by its
            # deadline. The adapter failure travels along as the cause.
            raise ledger_exc from exc
        if _recorded(row):
            return result_of(row)
        outcome, code = _outcome_after(row, exc)
        details = {}
    # Recording is outside the attempt on purpose: a failure here is raised as itself and
    # leaves the row as the attempt left it (a receipt stays a receipt; recovery reads it
    # again), instead of being mistaken for an adapter failure and re-derived into a
    # different outcome.
    return await complete(outcome, code, **details)
