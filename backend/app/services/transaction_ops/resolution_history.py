"""Bounded, tenant-scoped resolution evidence for operators and agents.

Examples are historical evidence only. They never manufacture proposals, copy
approval authority, or infer that an operation receipt resolved an exception.
"""

from decimal import Decimal, InvalidOperation

from sqlalchemy import String, and_, cast, exists, func, select
from sqlalchemy.orm import aliased

from app.models.transaction_ops import (
    TransactionCase,
    TransactionCaseObservation,
    TransactionFinding,
    TransactionOperation,
    TransactionProposal,
    TransactionRun,
)
from app.models.user import User
from app.services.transaction_ops import case_service
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.planner import source_fingerprint
from app.services.transaction_ops.settlement import is_settlement

_SCOPE_KEYS = ("source_connection_id", "source_step_id", "netsuite_account_id", "subsidiary_id", "record_type")


def _scope(snapshot):
    scope = {key: snapshot.get(key) for key in _SCOPE_KEYS}
    scope["netsuite_account_id"] = str(scope["netsuite_account_id"]).replace("_", "-").lower()
    return scope


def _scope_filters(case):
    filters = []
    for key in _SCOPE_KEYS:
        column = TransactionRun.config_snapshot[key].astext
        if key == "netsuite_account_id":
            column = func.lower(func.replace(column, "_", "-"))
        filters.append(column == case.scope_json.get(key))
    return filters


def _issue(report):
    """Compare financial dimensions, never customer text or approximate amounts."""
    try:
        balance = report["balance"]
        currency = balance["currency"]
        if not isinstance(currency, str) or len(currency) != 3:
            return None
        changed = []
        for key in ("order_total", "tax", "refunds"):
            value = balance["amounts"][key]["delta"]
            if value is None:
                continue
            if not isinstance(value, str) or not Decimal(value).is_finite():
                return None
            if Decimal(value) != 0:
                changed.append(key)
        return currency, balance["status"], tuple(changed), tuple(sorted(balance["missing_metrics"]))
    except (KeyError, TypeError, InvalidOperation):
        return None


def _query(tenant_id, case):
    return (
        select(TransactionProposal, TransactionRun, TransactionOperation, User.full_name)
        .join(
            TransactionRun, and_(TransactionRun.id == TransactionProposal.run_id, TransactionRun.tenant_id == tenant_id)
        )
        .outerjoin(
            TransactionOperation,
            and_(
                TransactionOperation.proposal_id == TransactionProposal.id, TransactionOperation.tenant_id == tenant_id
            ),
        )
        .outerjoin(User, and_(User.id == TransactionProposal.decided_by, User.tenant_id == tenant_id))
        .where(TransactionProposal.tenant_id == tenant_id, *_scope_filters(case))
        .order_by(TransactionProposal.created_at.desc(), TransactionProposal.id.desc())
    )


def _same_verified_state(operation, finding):
    """Do not credit an earlier fix for a later, different source/target state."""
    try:
        proof = operation.result_json["verification"]
        current = finding.report_json
        if proof.get("source_unchanged") is not True or len(current["targets"]) != 1:
            return False
        if proof.get("evidence_retention") == "summary_and_digests":
            target = current["targets"][0]
            summary = {key: val for key, val in target.items() if key not in {"lines", "tax_details", "observed_at"}}
            summary.update(line_count=len(target["lines"]), tax_component_count=len(target["tax_details"]))
            previous = {key: val for key, val in proof["target_observation"].items() if key != "observed_at"}
            return source_fingerprint(current["source"]) == proof["source_fingerprint"] and summary == previous
        previous = proof["report"]
        return (
            len(previous["targets"]) == 1
            and source_fingerprint(current["source"]) == source_fingerprint(previous["source"])
            and source_fingerprint(current["targets"][0]) == source_fingerprint(previous["targets"][0])
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


async def _project(db, tenant_id, records):
    keys = [
        state.business_digest({"settlement_operation": operation.id}) for _, _, operation, _ in records if operation
    ]
    checks = (
        {
            run.work_key: run
            for run in await db.scalars(
                select(TransactionRun).where(
                    TransactionRun.tenant_id == tenant_id,
                    TransactionRun.work_key.in_(keys),
                )
            )
        }
        if keys
        else {}
    )
    findings = (
        {
            str(row.id): row
            for row in await db.scalars(
                select(TransactionFinding).where(
                    TransactionFinding.tenant_id == tenant_id,
                    TransactionFinding.run_id.in_([run.id for run in checks.values()]),
                )
            )
        }
        if checks
        else {}
    )
    case_keys = [
        state.business_digest({**_scope(run.config_snapshot), "order_reference": proposal.order_reference})
        for proposal, run, _, _ in records
    ]
    cases = (
        {
            case.case_key: case
            for case in await db.scalars(
                select(TransactionCase).where(
                    TransactionCase.tenant_id == tenant_id,
                    TransactionCase.case_key.in_(case_keys),
                )
            )
        }
        if case_keys
        else {}
    )
    rows = []
    for proposal, original_run, operation, name in records:
        check = checks.get(state.business_digest({"settlement_operation": operation.id})) if operation else None
        result = (check.progress_json or {}).get("settlement") or {} if check else {}
        case_key = state.business_digest(
            {**_scope(original_run.config_snapshot), "order_reference": proposal.order_reference}
        )
        case = cases.get(case_key)
        finding = findings.get(result.get("finding_id"))
        verified = bool(
            proposal.status == "approved"
            and proposal.decided_by
            and operation
            and operation.status == "verified"
            and ((operation.result_json or {}).get("verification") or {}).get("source_unchanged") is True
            and check
            and is_settlement(check)
            and check.config_id == proposal.config_id
            and check.status == "finished"
            and check.termination_reason == "done"
            and check.params_json.get("order_references") == [proposal.order_reference]
            and result.get("status") == "succeeded"
            and result.get("operation_id") == str(operation.id)
            and result.get("proposal_id") == str(proposal.id)
            and result.get("approved_by") == str(proposal.decided_by)
            and result.get("evidence_fingerprint") == proposal.evidence_fingerprint
            and case
            and case.status == "reconciled"
            and result.get("case_id") == str(case.id)
            and finding
            and finding.run_id == check.id
            and _same_verified_state(operation, finding)
        )
        rows.append(
            {
                "proposal_id": str(proposal.id),
                "run_id": str(original_run.id),
                "case_id": str(case.id) if case else None,
                "order_reference": proposal.order_reference,
                "action": proposal.action,
                "currency": proposal.currency,
                "proposal_status": proposal.status,
                "evidence_fingerprint": proposal.evidence_fingerprint,
                "decided_by": str(proposal.decided_by) if proposal.decided_by else None,
                "decided_by_name": name,
                "decided_at": proposal.decided_at.isoformat() if proposal.decided_at else None,
                "approved_by": str(proposal.decided_by) if proposal.status == "approved" else None,
                "approved_by_name": name if proposal.status == "approved" else None,
                "operation_id": str(operation.id) if operation else None,
                "operation_status": operation.status if operation else "not_executed",
                "settlement_run_id": str(check.id) if check else None,
                "settlement_status": result.get("status", check.status) if check else "not_evaluated",
                "verification_finding_id": result.get("finding_id"),
                "verified_example": verified,
                "requires_new_human_approval": True,
                "proposal_url": f"/api/v1/transaction-ops/proposals/{proposal.id}",
                "review_url": f"/transaction-operations/runs/{original_run.id}",
            }
        )
    return rows


async def history(db, tenant_id, case_id, *, limit=10, offset=0):
    case = await case_service.get_case(db, tenant_id, case_id)
    limit, offset = min(25, max(1, limit)), max(0, offset)
    records = (
        await db.execute(
            _query(tenant_id, case)
            .where(
                TransactionProposal.order_reference == case.order_reference,
            )
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    rows = await _project(db, tenant_id, records[:limit])
    # The active evidence's exact mapping is the compatibility boundary. A
    # configuration revision cannot silently inherit an old repair recipe.
    current_run = await db.scalar(
        select(TransactionRun)
        .join(
            TransactionCaseObservation,
            and_(
                TransactionCaseObservation.run_id == TransactionRun.id,
                TransactionCaseObservation.tenant_id == tenant_id,
            ),
        )
        .where(TransactionRun.tenant_id == tenant_id, TransactionCaseObservation.case_id == case.id)
        .order_by(TransactionCaseObservation.observed_at.desc(), TransactionCaseObservation.id.desc())
        .limit(1)
    )
    issue = _issue(case.latest_report_json)
    candidates = []
    if current_run and issue is not None:
        verified_run = aliased(TransactionRun)
        financially_verified = exists(
            select(verified_run.id).where(
                verified_run.tenant_id == tenant_id,
                verified_run.origin == "recovery",
                verified_run.status == "finished",
                verified_run.termination_reason == "done",
                verified_run.config_id == TransactionProposal.config_id,
                verified_run.params_json["operation_id"].astext == cast(TransactionOperation.id, String),
                verified_run.params_json["verification_scope"].astext == "order_total_tax_refunds",
                verified_run.progress_json["settlement"]["status"].astext == "succeeded",
            )
        )
        resolved_case = exists(
            select(TransactionCase.id).where(
                TransactionCase.tenant_id == tenant_id,
                TransactionCase.order_reference == TransactionProposal.order_reference,
                TransactionCase.scope_json == case.scope_json,
                TransactionCase.status == "reconciled",
            )
        )
        candidates = (
            await db.execute(
                _query(tenant_id, case)
                .where(
                    TransactionProposal.order_reference != case.order_reference,
                    TransactionProposal.currency == issue[0],
                    TransactionProposal.status == "approved",
                    TransactionOperation.status == "verified",
                    financially_verified,
                    resolved_case,
                    TransactionRun.config_snapshot["mapping_json"] == current_run.config_snapshot.get("mapping_json"),
                )
                .limit(26)
            )
        ).all()
    applicable = [record for record in candidates[:25] if _issue(record[0].evidence_json.get("report") or {}) == issue]
    examples = [row for row in await _project(db, tenant_id, applicable) if row["verified_example"]]
    return {
        "case_id": str(case.id),
        "resolutions": rows,
        "truncated": len(records) > limit,
        "next_offset": offset + limit if len(records) > limit else None,
        "examples": examples[:5],
        "examples_truncated": len(candidates) > 25 or len(examples) > 5,
        "usage": "Historical examples inform investigation only. "
        "Re-read current evidence and obtain a new exact human approval.",
    }
