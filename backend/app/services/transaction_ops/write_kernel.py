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

from sqlalchemy import select

from app.core.database import set_tenant_context
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
    """A read found a reason to stop (the source order's payment failed, an unsupported
    action). Ends the attempt without a send; the message is the ledger code when it is
    a documented stop, otherwise the generic revalidation code."""


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


async def _operation(db, tenant_id, operation_id):
    await set_tenant_context(db, str(tenant_id))
    query = select(TransactionOperation).where(
        TransactionOperation.tenant_id == tenant_id, TransactionOperation.id == operation_id
    )
    return (await db.execute(query.execution_options(populate_existing=True))).scalar_one_or_none()


def result_of(row) -> dict:
    return {
        "operation_id": str(row.id),
        "status": row.status,
        "termination_reason": (row.result_json or {}).get("termination_reason", "stall"),
    }


async def execute(db, tenant_id, claimed, adapter: WriteAdapter, *, clock=None) -> dict:
    """Run a claimed operation through its adapter and record the outcome.

    The claim (state_service.claim_approved_operation) has already committed the executing
    row. A duplicate delivery of the same claim reads the durable outcome; it never obtains
    another permit, because the permit and the receipt are one-use on the row itself.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    stop_codes = {"source_payment_failed", "unsupported_action"}

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
        row = await _operation(db, tenant_id, claimed.operation_id)
        if row.status not in ("executing", "committed_unverified"):
            return result_of(row)
        row = await state.complete_operation(
            db, tenant_id, row.id, outcome=outcome, result_json={"code": code, **details}, now=clock()
        )
        return result_of(row)

    receipt = None
    try:
        preflight = await adapter.preflight(db, tenant_id, claimed, read=read)
        receipt = await adapter.send(db, tenant_id, claimed, preflight)
        if receipt["status"] == "failed":
            return await complete("rejected_before_effect", "provider_rejected_without_save")
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
                return await complete("needs_review", "adapter_receipt_without_permit")
        proof = await adapter.verify(db, tenant_id, claimed, preflight, read=read)
        if proof is not None:
            return await complete("verified", "independently_verified", verification=proof)
        # A receipt without proof stays committed_unverified (readback again later); no
        # receipt at all is unknown (existence must be reconciled first). Never a resend.
        return await complete(
            "committed_unverified" if receipt["status"] == "accepted" else "unknown", "verification_unproven"
        )
    except PreconditionChangedError as exc:
        row = await _operation(db, tenant_id, claimed.operation_id)
        if row.status not in ("executing", "committed_unverified"):
            return result_of(row)
        if (row.result_json or {}).get("dispatch_reserved") is True:
            # Preflight must never reserve; if an adapter did, the send may have happened.
            return await complete("unknown", exc.code)
        return await complete("rejected_before_effect", exc.code)
    except Exception as exc:
        # A transport exception may follow an actual send; only the committed ledger
        # decides whether the failure is known. Never expose raw exceptions.
        row = await _operation(db, tenant_id, claimed.operation_id)
        if row.status not in ("executing", "committed_unverified"):
            return result_of(row)
        if row.status == "committed_unverified":
            return await complete("committed_unverified", "verification_unavailable")
        sent = (row.result_json or {}).get("dispatch_reserved") is True
        code = "verification_unavailable" if sent else "evidence_revalidation_failed"
        if isinstance(exc, ExecutionStoppedError) and str(exc) in stop_codes:
            code = str(exc)
        return await complete("unknown" if sent else "rejected_before_effect", code)
