"""Human-approved, bounded execution with independent outcome verification.

A duplicate delivery reads durable status. It never obtains another send permit.
Missing/failed verification after a send is unknown, including a successful HTTP
receipt. Unknown outcomes are handed to a separate read-only recovery job.
"""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.database import set_tenant_context
from app.models.transaction_ops import TransactionOperation
from app.services.transaction_ops import state_service as state
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
    read_guard_snapshot,
)
from app.services.transaction_ops.normalization import TransactionMapping, _time
from app.services.transaction_ops.planner import plan_proposal, source_fingerprint
from app.services.transaction_ops.runner import build_report
from app.services.transaction_ops.source_reader import read_framework_order


class ExecutionStoppedError(ValueError):
    pass


async def _operation(db, tenant_id, proposal, *, operation_id=None):
    await set_tenant_context(db, str(tenant_id))
    query = select(TransactionOperation).where(TransactionOperation.tenant_id == tenant_id)
    query = (
        query.where(TransactionOperation.id == operation_id)
        if operation_id
        else query.where(TransactionOperation.work_key == proposal.work_key)
    )
    return (await db.execute(query.execution_options(populate_existing=True))).scalar_one_or_none()


def _result(row):
    return {
        "operation_id": str(row.id),
        "status": row.status,
        "termination_reason": (row.result_json or {}).get("termination_reason", "stall"),
    }


def verify_outcome(proposal, report, *, guard=None, resolution=None):
    """Proof of the approved desired state, never proof inferred from a receipt."""
    evidence = proposal.evidence_json
    if (
        evidence.get("schema_version") != 1
        or source_fingerprint(report["source"]) != evidence.get("source_fingerprint")
        or report["comparison"]["recommended_action"] != "no_action"
        or len(report["targets"]) != 1
        or report["targets"][0]["record_id"] != proposal.target_record_id
    ):
        return None
    proof = {"source_unchanged": True, "report": report}
    if proposal.action == "correct_amounts":
        if not guard or not isinstance(guard.get("snapshot"), dict):
            return None
        expected = deepcopy(proposal.before_json)
        expected.update(proposal.after_json["body_changes"])
        expected.update(proposal.after_json["expected_totals"])
        lines = {line["line"]: line for line in expected["lines"]}
        for change in proposal.after_json["line_changes"]:
            lines[change["line"]].update(change["fields"])
        actual = deepcopy(guard["snapshot"])
        if _time(actual.get("version")) != _time(report["targets"][0]["updated_at"]):
            return None
        expected.pop("version")
        actual.pop("version")
        if actual != expected:
            return None
        proof["guard"] = guard
    elif proposal.action == "resolve_celigo_error":
        approved = evidence.get("celigo") or {}
        if not resolution or (
            resolution.get("complete") is not True
            or resolution.get("resolved") is not True
            or resolution.get("error_id") != (approved.get("error") or {}).get("error_id")
            or resolution.get("order_reference") != proposal.order_reference
            or resolution.get("scope") != approved.get("scope")
            or resolution.get("config_fingerprint") != approved.get("config_fingerprint")
        ):
            return None
        proof["celigo"] = resolution
    else:
        return None
    return proof


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
    scope = {key: getattr(config, key) for key in ("netsuite_account_id", "subsidiary_id", "record_type")}

    async def read(cost, function, *args, **kwargs):
        permit = await state.reserve_operation_budget(db, tenant_id, claimed.operation_id, api_calls=cost, now=clock())
        if permit is None:
            raise ExecutionStoppedError("operation_budget_exhausted")
        seconds = (permit.deadline_at - clock()).total_seconds()
        if seconds <= 0:
            raise ExecutionStoppedError("operation_budget_exhausted")
        async with asyncio.timeout(min(seconds, 120)):
            return await function(db, tenant_id, *args, **kwargs)

    async def pair():
        source = await read(2, read_framework_order, config.source_step_id, proposal.order_reference)
        targets = await read(
            10,
            read_netsuite_order,
            config.netsuite_connection_id,
            config.netsuite_account_id,
            config.subsidiary_id,
            proposal.order_reference,
            mapping.reference_field,
        )
        report = build_report(source, targets, scope, mapping, now=clock())
        if report["order_reference"] != proposal.order_reference:
            raise ExecutionStoppedError("source_identity_changed")
        return targets, report

    async def complete(outcome, code, **details):
        row = await _operation(db, tenant_id, proposal, operation_id=claimed.operation_id)
        if row.status != "executing":
            return _result(row)
        row = await state.complete_operation(
            db, tenant_id, row.id, outcome=outcome, result_json={"code": code, **details}, now=clock()
        )
        return _result(row)

    try:
        targets, report = await pair()
        guard = celigo = None
        if claimed.action == "correct_amounts":
            guard = await read(MAX_GUARD_READ_CALLS, read_guard_snapshot, config, claimed.target_record_id)
        elif claimed.action == "resolve_celigo_error":
            celigo = await read(
                MAX_READ_CALLS,
                read_celigo_error_evidence,
                config.target_step_id,
                proposal.order_reference,
                error_id=proposal.before_json["celigo_error_id"],
            )
        else:
            raise ExecutionStoppedError("unsupported_action")
        fresh = plan_proposal(report, targets, config, now=clock(), guard=guard, celigo=celigo)
        if (
            fresh.evidence_fingerprint != proposal.evidence_fingerprint
            or fresh.before_json != claimed.before_json
            or fresh.after_json != claimed.after_json
        ):
            return await complete("failed", "approved_evidence_changed")
        # Adapters own the final live guard + committed one-use send reservation.
        if claimed.action == "correct_amounts":
            receipt = await dispatch_netsuite_operation(db, tenant_id, claimed)
        else:
            receipt = await dispatch_celigo_resolution(db, tenant_id, claimed, celigo)
        if receipt["status"] == "failed":
            return await complete("failed", "provider_rejected_without_save")
        _, report = await pair()
        guard = resolution = None
        if claimed.action == "correct_amounts":
            guard = await read(MAX_GUARD_READ_CALLS, read_guard_snapshot, config, claimed.target_record_id)
        else:
            resolution = await read(
                MAX_READ_CALLS, read_celigo_resolution, config.target_step_id, proposal.evidence_json["celigo"]
            )
        proof = verify_outcome(proposal, report, guard=guard, resolution=resolution)
        if proof is not None:
            return await complete("verified", "independently_verified", verification=proof)
        return await complete("unknown", "verification_unproven")
    except Exception:
        # A transport exception may follow an actual send; only the committed
        # ledger decides whether failure is known. Never expose raw exceptions.
        row = await _operation(db, tenant_id, proposal, operation_id=claimed.operation_id)
        if row.status != "executing":
            return _result(row)
        sent = (row.result_json or {}).get("dispatch_reserved") is True
        return await complete(
            "unknown" if sent else "failed", "verification_unavailable" if sent else "evidence_revalidation_failed"
        )
