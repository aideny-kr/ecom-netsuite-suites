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
from app.services.transaction_ops import accounting_credit_recheck
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.case_service import _cleared
from app.services.transaction_ops.settlement import SCOPE
from app.services.transaction_ops.treatments import is_mcp, reconciliation_target_id, supports, treatment_of

# Provider-call ceiling of one recheck run. The MCP existing-credit recheck adds the
# subledger read budget it reserves in accounting_credit_recheck plus a small headroom,
# so the two numbers cannot drift apart: raising READ_CALLS raises the ceiling that has
# to afford it. 64 + 56 + 8 preserves the 128 the previous literal allowed.
RECHECK_CALLS = 64
MCP_RECHECK_HEADROOM = 8


def needs_subledger_recheck(proposal):
    """The one recheck that re-reads the subledger: an existing-credit correction sent over MCP."""
    return proposal.get("kind") == "credit_tax_reallocation" and is_mcp(proposal)


def recheck_call_ceiling(proposal):
    # Keyed on transport, not kind: every MCP-transported recheck kept the larger
    # ceiling before, and narrowing it to one kind would halve the budget of the
    # others without any change in what they read.
    if is_mcp(proposal):
        return RECHECK_CALLS + accounting_credit_recheck.READ_CALLS + MCP_RECHECK_HEADROOM
    return RECHECK_CALLS


async def queue(db, tenant_id, message, actor_id, *, now, config_id=None):
    """``config_id`` is the config the card's ledger claim recorded as its recovery scope,
    for a card whose review does not name one (the invoice-tax builder never does)."""
    so = message.structured_output or {}
    p = so.get("accounting_review") or {}
    verification = so.get("accounting_verification") or {}
    if (
        message.tenant_id != tenant_id
        or p.get("tenant_id") != str(tenant_id)
        or not supports(p)
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
        effective = config_id or effective_config_id(so)
        if not effective:
            raise state.StateError("accounting_recheck_unscoped")
        config = await state.get_config(db, tenant_id, UUID(str(effective)))
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
            max_api_calls=min(config.max_api_calls, recheck_call_ceiling(p)),
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
        or not supports(p)
        or str(run.config_id) != str(effective_config_id(so) or "")
        or run.params_json["order_references"] != [p.get("order_reference")]
    ):
        raise state.StateError("accounting_recheck_approval_mismatch")
    return message, p


def effective_config_id(so) -> str | None:
    """The config a card's recheck runs under: the one its review names, else the one its
    ledger claim recorded as the recovery scope (a card whose builder set none)."""
    p = so.get("accounting_review") or {}
    claim = so.get("accounting_execution") or {}
    return p.get("config_id") or ((claim.get("recovery_scope") or {}).get("config_id"))


def report_in_scope(run, p, report, now):
    try:
        targets = report["targets"]
        verified_at = datetime.fromisoformat(run.params_json["verified_at"])
        # A declared reconciliation target wins; otherwise the treatment's rule. A
        # credit can be created from an invoice, so it binds through the collected
        # invoice -> sales-order edge and refuses when that edge disagrees.
        target_id = reconciliation_target_id(p)
        if target_id is None:
            return False
        treatment = treatment_of(p)
        return (
            len(targets) == 1
            and str(targets[0]["record_id"]) == str(target_id)
            and str(report["source"]["record_id"]) == str(p["source"]["id"])
            and report["balance"]["currency"]
            == (p["profile"]["currency"] if treatment.family == "commercial" else p["source"]["currency"])
            and all(
                verified_at <= datetime.fromisoformat(value["observed_at"]) <= now
                for value in (report["source"], targets[0])
            )
        )
    except (KeyError, TypeError, ValueError, IndexError):
        return False


async def bound_report(db, tenant_id, run, report, *, now, subledger_recheck=True):
    """Bind a recheck run's report to its approval.

    The scope check is cheap and runs on every write, so an interim finding never
    sits in the findings list unannotated. The subledger recheck reserves provider
    budget and re-reads NetSuite, so the caller asks for it only on the final write.
    """
    not_verified = accounting_credit_recheck.not_verified_report
    try:
        _, p = await approval_for_run(db, tenant_id, run)
    except state.StateError as exc:
        # The approval this run was queued for no longer matches, or its message is
        # gone. Binding fails closed on the report; the run itself must still
        # terminate normally, so this never raises out of a finding write. Only this
        # module's own codes are published; any other lookup failure gets one reason.
        code = getattr(exc, "code", None) or str(exc)
        if not code.startswith("accounting_recheck_"):
            code = "accounting_recheck_approval_unavailable"
        return not_verified(report, code)
    if not report_in_scope(run, p, report, now):
        return not_verified(report, "accounting_recheck_identity_or_freshness_unverified")
    if subledger_recheck and needs_subledger_recheck(p):
        return await accounting_credit_recheck.reconcile(db, tenant_id, run, p, report)
    return report


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
    from app.services.transaction_ops.accounting_completion import enqueue

    enqueue(message, run, now)
    await state._audit(db, tenant_id, "accounting_recheck.complete", run, payload=result)
