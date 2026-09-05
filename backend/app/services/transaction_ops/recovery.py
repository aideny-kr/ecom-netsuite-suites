"""One durable read-only reconciliation pass for an unknown external write."""

import asyncio
from datetime import datetime, timezone
from uuid import UUID

from app.models.transaction_ops import TransactionOperation
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.celigo_actions import MAX_READ_CALLS, read_celigo_resolution
from app.services.transaction_ops.executor import _result, verify_outcome
from app.services.transaction_ops.netsuite_reader import read_netsuite_order
from app.services.transaction_ops.netsuite_transport import MAX_GUARD_READ_CALLS, read_guard_snapshot
from app.services.transaction_ops.normalization import TransactionMapping
from app.services.transaction_ops.runner import build_report, enabled
from app.services.transaction_ops.source_reader import read_framework_order


async def recover_operation(db, tenant_id, operation_id, *, _clock=None):
    clock = _clock or (lambda: datetime.now(timezone.utc))
    await state.recover_expired_operation(db, tenant_id, operation_id, now=clock())
    operation = await state._one(db, tenant_id, TransactionOperation, operation_id)
    if operation.status != "unknown" or not await enabled(db, tenant_id):
        return _result(operation)
    proposal = await state.get_proposal(db, tenant_id, operation.proposal_id)
    if not (await state.get_config(db, tenant_id, proposal.config_id)).enabled:
        return _result(operation)
    run = await state.create_operation_recovery(db, tenant_id, operation_id, now=clock())
    return await reconcile_operation_run(db, tenant_id, run.id, _clock=clock)


async def reconcile_operation_run(db, tenant_id, run_id, *, _clock=None):
    clock = _clock or (lambda: datetime.now(timezone.utc))
    run = await state.get_run(db, tenant_id, run_id)
    if run.origin != "recovery":
        raise state.StateError("not_a_recovery_run")
    operation_id = UUID(run.params_json["operation_id"])
    operation = await state._one(db, tenant_id, TransactionOperation, operation_id)
    token = await state.claim_run(db, tenant_id, run_id, now=clock())
    if token is None:
        return _result(operation)
    proposal = await state.get_proposal(db, tenant_id, operation.proposal_id)
    if run.config_id != proposal.config_id or run.params_json["order_references"] != [proposal.order_reference]:
        raise state.StateError("recovery_scope_mismatch")
    config = await state.get_config(db, tenant_id, proposal.config_id)
    mapping = TransactionMapping.model_validate(config.mapping_json)
    reason, proof = "stall", None

    async def read(cost, function, *args, orders=0):
        if not await enabled(db, tenant_id) or not (await state.get_config(db, tenant_id, config.id)).enabled:
            raise state.StateError("recovery_disabled")
        permit = await state.reserve_budget(
            db, tenant_id, run_id, lease_token=token, api_calls=cost, orders=orders, now=clock()
        )
        if not permit:
            raise TimeoutError
        remaining = (run.deadline_at - clock()).total_seconds()
        if remaining <= 0:
            raise TimeoutError
        async with asyncio.timeout(min(remaining, 170)):
            return await function(db, tenant_id, *args)

    try:
        if operation.status == "unknown":
            source = await read(2, read_framework_order, config.source_step_id, proposal.order_reference, orders=1)
            targets = await read(
                10,
                read_netsuite_order,
                config.netsuite_connection_id,
                config.netsuite_account_id,
                config.subsidiary_id,
                proposal.order_reference,
                mapping.reference_field,
            )
            scope = {key: getattr(config, key) for key in ("netsuite_account_id", "subsidiary_id", "record_type")}
            report = build_report(source, targets, scope, mapping, now=clock())
            guard = resolved = None
            if proposal.action == "correct_amounts":
                guard = await read(MAX_GUARD_READ_CALLS, read_guard_snapshot, config, proposal.target_record_id)
            elif proposal.action == "resolve_celigo_error":
                resolved = await read(
                    MAX_READ_CALLS, read_celigo_resolution, config.target_step_id, proposal.evidence_json["celigo"]
                )
            proof = verify_outcome(proposal, report, guard=guard, resolution=resolved)
            await state.record_finding(
                db, tenant_id, run_id, proposal.order_reference, report, lease_token=token, now=clock()
            )
            reason = "done" if proof is not None else "stall"
        else:
            reason = "done"
    except TimeoutError:
        reason = "budget"
    except state.StateError as exc:
        if exc.code == "run_lease_lost":
            return _result(await state._one(db, tenant_id, TransactionOperation, operation_id))
        reason = "stall"
    except Exception:
        reason = "error"
    operation = await state.finish_operation_recovery(
        db, tenant_id, run_id, lease_token=token, reason=reason, proof=proof, now=clock()
    )
    return _result(operation)
