"""Durable, read-only case reconciliation after an audited chat correction.

Queued in the confirmation transaction. The existing recovery-run scheduler and
settlement reader execute it without proposal planning or financial writes.
"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from app.models.chat import ChatMessage
from app.models.transaction_ops import TransactionFinding, TransactionRun
from app.schemas.transaction_runs import ConfigOut
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.case_service import _cleared
from app.services.transaction_ops.settlement import SCOPE


async def queue(db, tenant_id, message, actor_id, *, now):
    so = message.structured_output or {}
    p = so.get("accounting_review") or {}
    verification = so.get("accounting_verification") or {}
    if (
        message.tenant_id != tenant_id
        or p.get("tenant_id") != str(tenant_id)
        or p.get("kind") not in {"sales_adjustment_credit", "invoice_sales_adjustment"}
        or so.get("status") != "approved"
        or verification.get("status") != "verified"
        or not actor_id
        or now.utcoffset() is None
    ):
        raise state.StateError("accounting_recheck_requires_verified_approval")
    key = state.business_digest({"accounting_recheck_confirmation": str(message.id)})
    # A failed queue cannot roll back the already recorded native-write outcome.
    # The caller retains a truthful unavailable status and can retry this read.
    async with db.begin_nested():
        existing = await db.scalar(
            select(TransactionRun).where(TransactionRun.tenant_id == tenant_id, TransactionRun.work_key == key)
        )
        if existing:
            return existing
        config = await state.get_config(db, tenant_id, UUID(p["config_id"]))
        snapshot = ConfigOut.model_validate(config).model_dump(mode="json")
        for field in ("source_connection_id", "source_step_id", "netsuite_account_id", "subsidiary_id", "record_type"):
            actual, approved = snapshot.get(field), p["scope"].get(field)
            if field == "netsuite_account_id":
                actual = str(actual).replace("_", "-").lower()
                approved = str(approved).replace("_", "-").lower()
            if actual != approved:
                raise state.StateError("accounting_recheck_scope_changed")
        row = TransactionRun(
            tenant_id=tenant_id,
            config_id=config.id,
            work_key=key,
            origin="recovery",
            params_json={
                "approval_message_id": str(message.id),
                "approved_by": str(actor_id),
                "verified_at": now.isoformat(),
                "verification_scope": SCOPE,
                "order_references": [p["order_reference"]],
            },
            config_snapshot=snapshot,
            max_api_calls=min(config.max_api_calls, 64),
            max_orders=1,
            deadline_at=now + timedelta(seconds=config.deadline_seconds),
            progress_json={},
            initiated_by=actor_id,
        )
        db.add(row)
        await db.flush()
        await state._audit(
            db,
            tenant_id,
            "accounting_recheck.create",
            row,
            payload={**row.params_json, "case_id": p["case_id"], "financial_writes": 0},
        )
        return row


async def approval_for_run(db, tenant_id, run):
    message = await state._one(db, tenant_id, ChatMessage, UUID(run.params_json["approval_message_id"]))
    so = message.structured_output or {}
    p = so.get("accounting_review") or {}
    if (
        so.get("status") != "approved"
        or (so.get("accounting_verification") or {}).get("status") != "verified"
        or p.get("tenant_id") != str(tenant_id)
        or p.get("kind") not in {"sales_adjustment_credit", "invoice_sales_adjustment"}
        or str(run.config_id) != p.get("config_id")
        or run.params_json["order_references"] != [p.get("order_reference")]
    ):
        raise state.StateError("accounting_recheck_approval_mismatch")
    return message, p


def report_in_scope(run, p, report, now):
    try:
        targets = report["targets"]
        verified_at = datetime.fromisoformat(run.params_json["verified_at"])
        return (
            len(targets) == 1
            and str(targets[0]["record_id"]) == str(p["before"]["createdFrom"]["id"])
            and str(report["source"]["record_id"]) == str(p["source"]["id"])
            and report["balance"]["currency"] == p["profile"]["currency"]
            and all(
                verified_at <= datetime.fromisoformat(value["observed_at"]) <= now
                for value in (report["source"], targets[0])
            )
        )
    except (KeyError, TypeError, ValueError, IndexError):
        return False


async def bound_report(db, tenant_id, run, report, *, now):
    _, p = await approval_for_run(db, tenant_id, run)
    if report_in_scope(run, p, report, now):
        return report
    return {
        **report,
        "balance": {
            **(report.get("balance") or {}),
            "status": "not_verified",
            "reason": "accounting_recheck_identity_or_freshness_unverified",
        },
        "evidence_limits": {"code": "accounting_recheck_identity_or_freshness_unverified"},
    }


async def record_outcome(db, tenant_id, run, reason, *, now):
    message, p = await approval_for_run(db, tenant_id, run)
    finding = await db.scalar(
        select(TransactionFinding).where(
            TransactionFinding.tenant_id == tenant_id,
            TransactionFinding.run_id == run.id,
            TransactionFinding.order_reference == p["order_reference"],
        )
    )
    report = finding.report_json if finding else {}
    verdict = "unverified"
    valid = reason == "done" and report_in_scope(run, p, report, now)
    if valid and _cleared(report, now):
        verdict = "succeeded"
    elif valid and report.get("balance", {}).get("status") == "difference":
        verdict = "difference"
    result = {
        "status": verdict,
        "verification_scope": SCOPE,
        "approval_message_id": str(message.id),
        "approved_by": run.params_json["approved_by"],
        "finding_id": str(finding.id) if finding else None,
        "case_id": p["case_id"],
        "checked_at": now.isoformat(),
        "termination_reason": reason,
        "cash_settlement": "not_verified",
    }
    run.progress_json = {**(run.progress_json or {}), "settlement": result}
    await state._audit(db, tenant_id, "accounting_recheck.complete", run, payload=result)
