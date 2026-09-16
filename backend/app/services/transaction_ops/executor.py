"""Human-approved, bounded execution with independent outcome verification.

A client of the write kernel (write_kernel.py): claim the approved proposal, pick the
adapter for its action, run the kernel loop. A duplicate delivery reads durable status.
It never obtains another send permit. Missing/failed verification after a send is
committed_unverified or unknown, including after a successful HTTP receipt; those are
handed to a separate read-only recovery job.

The provider reads and dispatches are module attributes on purpose: tests and drills
replace them here, and the adapters receive them through ``Reads`` at call time.
"""

from datetime import datetime, timezone

from app.services.transaction_ops import state_service as state
from app.services.transaction_ops import write_kernel
from app.services.transaction_ops.celigo_actions import (
    MAX_READ_CALLS,
    dispatch_celigo_resolution,
    read_celigo_error_evidence,
    read_celigo_resolution,
)
from app.services.transaction_ops.netsuite_reader import read_netsuite_order
from app.services.transaction_ops.netsuite_transport import (
    MAX_GUARD_READ_CALLS,
    dispatch_netsuite_operation,
    read_create_preview,
    read_created_snapshot,
    read_guard_snapshot,
)
from app.services.transaction_ops.normalization import TransactionMapping
from app.services.transaction_ops.source_reader import read_framework_order
from app.services.transaction_ops.write_adapters import Reads, build_adapter, verify_outcome
from app.services.transaction_ops.write_kernel import ExecutionStoppedError

__all__ = ["ExecutionStoppedError", "execute_proposal", "verify_outcome"]


def _reads() -> Reads:
    # Resolved at call time so a patched module attribute is what the adapter uses.
    return Reads(
        source=read_framework_order,
        target=read_netsuite_order,
        guard=read_guard_snapshot,
        create_preview=read_create_preview,
        created_snapshot=read_created_snapshot,
        dispatch_netsuite=dispatch_netsuite_operation,
        celigo_evidence=read_celigo_error_evidence,
        celigo_resolution=read_celigo_resolution,
        dispatch_celigo=dispatch_celigo_resolution,
        max_guard_calls=MAX_GUARD_READ_CALLS,
        max_celigo_calls=MAX_READ_CALLS,
    )


async def _operation(db, tenant_id, proposal, *, operation_id=None):
    if operation_id is not None:
        return await write_kernel._operation(db, tenant_id, operation_id)
    from sqlalchemy import select

    from app.core.database import set_tenant_context
    from app.models.transaction_ops import TransactionOperation

    await set_tenant_context(db, str(tenant_id))
    query = select(TransactionOperation).where(
        TransactionOperation.tenant_id == tenant_id, TransactionOperation.work_key == proposal.work_key
    )
    return (await db.execute(query.execution_options(populate_existing=True))).scalar_one_or_none()


_result = write_kernel.result_of


async def execute_proposal(db, tenant_id, proposal_id, *, _clock=None):
    clock = _clock or (lambda: datetime.now(timezone.utc))
    proposal = await state.get_proposal(db, tenant_id, proposal_id)
    try:
        claimed = await state.claim_approved_operation(
            db, tenant_id, proposal_id, expected_evidence_fingerprint=proposal.evidence_fingerprint, now=clock()
        )
    except state.StateError as exc:
        if exc.code == "operation_already_attempted":
            previous = await _operation(db, tenant_id, proposal)
            return _result(previous) if previous else {"status": "stalled", "termination_reason": "stall"}
        if exc.code == "proposal_not_approved":
            return {"status": proposal.status, "termination_reason": "stall"}
        raise
    if claimed is None:
        return {"status": "superseded", "termination_reason": "stall"}
    config = await state.get_config(db, tenant_id, claimed.config_id)
    mapping = TransactionMapping.model_validate(config.mapping_json)
    try:
        adapter = build_adapter(
            claimed.action, reads=_reads(), config=config, mapping=mapping, proposal=proposal, clock=clock
        )
    except ExecutionStoppedError:
        # No adapter, nothing read, nothing sent: the row is closed before any budget is spent.
        row = await state.complete_operation(
            db,
            tenant_id,
            claimed.operation_id,
            outcome="rejected_before_effect",
            result_json={"code": "unsupported_action"},
        )
        return _result(row)
    return await write_kernel.execute(db, tenant_id, claimed, adapter, clock=clock)
