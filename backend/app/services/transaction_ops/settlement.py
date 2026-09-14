"""One durable financial recheck after verified execution; never payment settlement.

Uses the existing recovery origin and immutable server-created scope. The normal
investigation readers collect gross, VAT and refunds without proposal planning.
Operation verification remains immutable; this run records the separate result.
"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from app.models.transaction_ops import TransactionFinding, TransactionOperation, TransactionRun
from app.schemas.transaction_runs import ConfigOut
from app.services.transaction_ops.case_service import _cleared

SCOPE = "order_total_tax_refunds"


def is_settlement(run):
    return getattr(run, "origin", None) == "recovery" and run.params_json.get("verification_scope") == SCOPE


async def queue(db, tenant_id, operation, proposal, *, now):
    """Called under the operation lock and committed with its verified outcome."""
    from app.services.transaction_ops import state_service as state

    if operation.status != "verified" or proposal.status != "approved":
        raise state.StateError("settlement_requires_verified_operation")
    key = state.business_digest({"settlement_operation": operation.id})
    existing = await db.scalar(
        select(TransactionRun).where(
            TransactionRun.tenant_id == tenant_id,
            TransactionRun.work_key == key,
        )
    )
    if existing is not None:
        return existing
    config = await state.get_config(db, tenant_id, proposal.config_id)
    row = TransactionRun(
        tenant_id=tenant_id,
        config_id=config.id,
        work_key=key,
        origin="recovery",
        params_json={
            "operation_id": str(operation.id),
            "verification_scope": SCOPE,
            "order_references": [proposal.order_reference],
        },
        config_snapshot=ConfigOut.model_validate(config).model_dump(mode="json"),
        max_api_calls=min(config.max_api_calls, 64),
        max_orders=1,
        deadline_at=now + timedelta(seconds=config.deadline_seconds),
        progress_json={},
        initiated_by=None,
    )
    db.add(row)
    await db.flush()
    await state._audit(
        db,
        tenant_id,
        "settlement.create",
        row,
        payload={
            "operation_id": str(operation.id),
            "proposal_id": str(proposal.id),
            "approved_by": str(proposal.decided_by),
            "approved_at": proposal.decided_at.isoformat(),
            "verification_scope": SCOPE,
        },
    )
    return row


async def record_outcome(db, tenant_id, run, reason, *, now):
    """Runs before the terminal transition, atomically with the run's audit."""
    from app.services.transaction_ops import state_service as state

    operation = await state._one(db, tenant_id, TransactionOperation, UUID(run.params_json["operation_id"]))
    proposal = await state.get_proposal(db, tenant_id, operation.proposal_id)
    if run.config_id != proposal.config_id or run.params_json["order_references"] != [proposal.order_reference]:
        raise state.StateError("settlement_scope_mismatch")
    finding = await db.scalar(
        select(TransactionFinding).where(
            TransactionFinding.tenant_id == tenant_id,
            TransactionFinding.run_id == run.id,
            TransactionFinding.order_reference == proposal.order_reference,
        )
    )
    report = finding.report_json if finding else {}
    balance = report.get("balance") or {}
    verdict = "unverified"
    try:
        target_id = proposal.target_record_id
        if not target_id:
            proof = (operation.result_json or {}).get("verification") or {}
            created = (proof.get("report") or {}).get("targets") or []
            target_id = created[0].get("record_id") if len(created) == 1 else None
            if not target_id and proof.get("evidence_retention") == "summary_and_digests":
                target_id = (proof.get("target_observation") or {}).get("record_id")
        targets = report["targets"]
        fresh = all(
            operation.completed_at <= datetime.fromisoformat(snapshot["observed_at"]) <= now
            and now - datetime.fromisoformat(snapshot["observed_at"]) <= timedelta(minutes=15)
            and snapshot.get("authoritative") is True
            for snapshot in (report["source"], targets[0])
        )
        valid = (
            reason == "done"
            and operation.status == "verified"
            and proposal.status == "approved"
            and report["order_reference"] == proposal.order_reference
            and balance["currency"] == proposal.currency
            and len(targets) == 1
            and target_id
            and targets[0]["record_id"] == target_id
            and fresh
            and report["lookup"].get("complete") is True
            and report["lookup"].get("authoritative") is True
        )
        if valid and _cleared(report, now):
            unsettled = await db.scalar(
                select(TransactionOperation.id)
                .where(
                    TransactionOperation.tenant_id == tenant_id,
                    TransactionOperation.entity_key == operation.entity_key,
                    TransactionOperation.status.in_(("executing", "unknown")),
                )
                .limit(1)
            )
            if unsettled is None:
                verdict = "succeeded"
        elif valid and balance.get("status") == "difference":
            verdict = "difference"
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        pass  # Missing evidence never establishes success.
    result = {
        "status": verdict,
        "verification_scope": SCOPE,
        "operation_id": str(operation.id),
        "proposal_id": str(proposal.id),
        "approved_by": str(proposal.decided_by),
        "approved_at": proposal.decided_at.isoformat(),
        "evidence_fingerprint": proposal.evidence_fingerprint,
        "finding_id": str(finding.id) if finding else None,
        "case_id": report.get("case_id"),
        "checked_at": now.isoformat(),
        "termination_reason": reason,
    }
    run.progress_json = {**(run.progress_json or {}), "settlement": result}
    await state._audit(db, tenant_id, "settlement.complete", run, payload=result)


async def status(db, tenant_id, operation_id):
    from app.services.transaction_ops import state_service as state

    operation = await state._one(db, tenant_id, TransactionOperation, operation_id)
    run = await db.scalar(
        select(TransactionRun).where(
            TransactionRun.tenant_id == tenant_id,
            TransactionRun.work_key == state.business_digest({"settlement_operation": operation.id}),
        )
    )
    return {
        "operation_id": str(operation.id),
        "operation_status": operation.status,
        "run_id": str(run.id) if run else None,
        "status": ((run.progress_json or {}).get("settlement") or {}).get("status", run.status)
        if run
        else "not_evaluated",
        "verification_scope": SCOPE,
        "result": (run.progress_json or {}).get("settlement") if run else None,
    }
