"""Read-only, approval-bound invoice-minus-credit reconciliation.

Raw order evidence is retained. Only a proved existing-credit correction with
protected original records can use this posting basis; cash is never certified.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.database import set_tenant_context
from app.services.transaction_ops import credit_api_correction
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.native_accounting_service import _stable
from app.services.transaction_ops.posting_balance import repriced_credit_balance
from app.services.transaction_ops.source_reader import SourceReadError

READ_CALLS = 56  # Accounting (20), support (24), source and OAuth maintenance allowance.
READ_SESSION_FACTORY = "accounting_authorization_session_factory"  # the dispatcher's worker-safe factory key


def _read_session(db):
    """An independent session for the provider read.

    A statement cancelled by the timeout is treated by SQLAlchemy as a disconnect: the
    connection is invalidated, and a session sharing it is unrecoverable until it rolls
    back. A defect after a query leaves the transaction aborted with the same remedy. On
    the caller's session that rollback expires every row the caller holds, including the
    locked run that record_finding reads next. So the read runs on its own session and
    connection, and whatever a failed read leaves behind is discarded with it. The
    session comes from the caller's own engine (never the app's global pool: Celery owns
    event-loop-local engines, and a pool created on another loop fails with "attached to
    a different loop"). A caller that already publishes a session factory under the
    dispatcher's key gets that one instead.
    """
    info = getattr(db, "info", None)
    factory = info.get(READ_SESSION_FACTORY) if isinstance(info, dict) else None
    return (factory or async_sessionmaker(db.bind, expire_on_commit=False))()


def project(p, report, current, *, verified_at, now):
    source, review, evidence, support = current
    if (
        str(support["credit"]["id"]) != p["record_id"]
        or str(support["invoice"]["id"]) != p["invoice_id"]
        or str(evidence["sections"]["sales_order"]["id"]) != p["sales_order_id"]
        or str(support["refund"]["id"]) != str(p["support"]["refund"]["id"])
        or str(support["book"]) != p["accounting_book"]
        or source != p["source"]
        or review["scope"] != p["scope"]
        or report["order_reference"] != p["order_reference"]
        or any(
            str(support[k]) != str(p[v])
            for k, v in (
                ("ar_account", "ar_account"),
                ("offset_account", "sales_adjustment_account"),
                ("tax_account", "tax_account"),
            )
        )
        or _stable(evidence["sections"]["sales_order"]) != _stable(p["protected_sales_order"])
        or any(_stable(support[k]) != _stable(p["support"][k]) for k in ("invoice", "refund", "invoice_gl"))
        or support["credit"].get("application_evidence") != p["support"]["credit"].get("application_evidence")
    ):
        raise ValueError("credit_recheck_identity_changed")
    refunds = report.get("refund_evidence") or {}
    amounts = (report.get("balance") or {}).get("amounts") or {}
    if not isinstance(refunds.get("target"), dict) or not all(
        isinstance(amounts.get(metric), dict) and "source" in amounts[metric] for metric in ("order_total", "tax")
    ):
        # A report that never collected refunds or amounts is missing evidence, not a defect.
        raise ValueError("credit_recheck_incomplete_report")
    for value in [support, evidence, *refunds.values()]:
        # The runner records an unavailable read as {"complete": False, "reason": ...}
        # with no observation time. That is missing evidence, never a defect.
        stamp = value.get("observed_at") if isinstance(value, dict) else None
        if not stamp:
            raise ValueError("credit_recheck_incomplete_evidence")
        observed = datetime.fromisoformat(stamp)
        if not verified_at <= observed <= now or now - observed > timedelta(minutes=15):
            raise ValueError("credit_recheck_stale_evidence")
    # The full fresh order and posting records must describe this same revision.
    for metric, key in (("order_total", "total"), ("tax", "tax_total")):
        if Decimal(amounts[metric]["source"]) != Decimal(source[key]):
            raise ValueError("credit_recheck_source_changed")
    target_refund = refunds["target"]
    if (
        target_refund.get("complete") is not True
        or target_refund.get("record_ids") != [str(support["refund"]["id"])]
        or target_refund.get("currency") != source["currency"]
        or target_refund.get("order_reference") != source["number"]
        or Decimal(target_refund["amount"]) != Decimal(support["refund"]["total"])
    ):
        raise ValueError("credit_recheck_refund_unverified")
    posting = repriced_credit_balance(source, review, evidence, support, report)
    if posting is None:
        raise ValueError("credit_recheck_posting_unverified")
    limits = report.get("evidence_limits")
    if limits and limits.get("code") not in {"detailed_evidence_unavailable", "evidence_size_limit"}:
        raise ValueError("credit_recheck_incomplete_report")
    return {
        **report,
        "balance": {
            **report["balance"],
            "status": posting["status"],
            "reason": "verified_invoice_less_existing_credit",
            "amounts": {k: posting["amounts"][k] for k in ("order_total", "tax", "refunds")},
            "missing_metrics": [],
            "original_order_comparison": report["balance"],
            "posting_reconciliation": posting,
        },
    }


# Failures of the evidence itself: a fresh read that could not complete, timed out, did
# not describe the approved revision, or came back in a shape this projection cannot
# read. The lookup, type and attribute errors are the repo's established signal for the
# last case (see commercial_credits, case_service, accounting_history): fresh() and
# project() consume externally shaped records, so a missing key there is missing
# evidence, not a defect. These degrade to not_verified. Anything else is a defect in
# this code: it is audited and committed like every other exit and then surfaced, never
# reclassified as missing evidence. Deciding this by exception type is a known
# compromise: the durable fix is one typed precondition error raised by the adapter
# (docs/superpowers/specs/2026-09-15-write-kernel-design.md, section 5).
EVIDENCE_FAILURES = (
    ValueError,
    LookupError,
    TypeError,
    AttributeError,
    ArithmeticError,
    TimeoutError,
    SourceReadError,
    httpx.HTTPError,
)


def not_verified_report(report, reason):
    """The one shape a recheck report takes when it cannot be verified."""
    return {
        **report,
        "balance": {**(report.get("balance") or {}), "status": "not_verified", "reason": reason},
        "evidence_limits": {"code": reason},
    }


def _observed(approval_message_id, p, balance, defect):
    """The posting_observed audit payload; every exit of reconcile() writes one."""
    payload = {
        "approval_message_id": approval_message_id,
        "case_id": p["case_id"],
        "financial_writes": 0,
        "balance": balance,
    }
    if defect is not None:
        payload["error_type"] = type(defect).__name__
    return payload


async def _leave(db, tenant_id, run, payload, error=None):
    """Audit the exit; when leaving with an error, commit the audit and raise.

    On a normal return the caller (state_service.record_finding) commits the finding
    and this audit together; committing here would split them. On a raise the caller's
    commit is never reached and the session would roll the audit back, so it is
    committed first. The only way out of reconcile().
    """
    await state._audit(db, tenant_id, "accounting_recheck.posting_observed", run, payload=payload)
    if error is not None:
        await state._commit(db, tenant_id)
        raise error


async def reconcile(db, tenant_id, run, p, report):
    now = datetime.now(timezone.utc)
    run_id, approval_message_id = run.id, run.params_json["approval_message_id"]
    result = None
    defect = None
    remaining = (run.deadline_at - now).total_seconds()
    if remaining <= 0 or run.api_calls_used + (run.api_calls_held or 0) + READ_CALLS > run.max_api_calls:
        reason = "credit_recheck_budget_exhausted"
    else:
        # Reserve before reading; a failure also consumes its reserved budget.
        run.api_calls_used += READ_CALLS
        await state._audit(db, tenant_id, "accounting_recheck.read_budget", run, payload={"api_calls": READ_CALLS})
        lease_token = run.lease_token
        await state._commit(db, tenant_id)
        try:
            async with _read_session(db) as read_db:
                # The timeout sits inside the session block, so the cancellation becomes
                # TimeoutError before the session closes and close() runs normally. The
                # first statement is also the pool checkout, so it sits inside the
                # timeout too: an exhausted pool is missing evidence, bounded like a
                # slow provider, not an unbounded wait.
                async with asyncio.timeout(min(90, remaining)):
                    await set_tenant_context(read_db, str(tenant_id))
                    current = await credit_api_correction.fresh(read_db, tenant_id, p)
            result = project(
                p,
                report,
                current,
                verified_at=datetime.fromisoformat(run.params_json["verified_at"]),
                now=datetime.now(timezone.utc),
            )
        except EVIDENCE_FAILURES as exc:
            # Never reuse the approval-time snapshot or raw order balance as a
            # successful posting recheck after incomplete fresh evidence. project()'s
            # own reasons survive to the audit; anything else is generic.
            text = str(exc)
            reason = text if isinstance(exc, ValueError) and text.startswith("credit_recheck_") else None
            reason = reason or "credit_recheck_evidence_unavailable"
        except Exception as exc:
            defect, reason = exc, "credit_recheck_internal_error"
        # Reservation releases the database lock during provider I/O. Recheck
        # ownership before publishing evidence or changing the case verdict —
        # on every exit, including the ones that will be re-raised below.
        try:
            run = await state.get_run(db, tenant_id, run_id, lock=True)
        except state.StateError as gone:
            gone.__cause__ = defect  # the run row itself is gone: nothing left to audit against
            raise
        try:
            state._lease(run, lease_token, datetime.now(timezone.utc))
        except state.StateError as lost:
            # Ownership moved during the read. This worker may not publish a verdict,
            # but the exit is still audited so a captured defect is never lost with it.
            lost.__cause__ = defect
            balance = not_verified_report(report, "credit_recheck_lease_lost")["balance"]
            await _leave(db, tenant_id, run, _observed(approval_message_id, p, balance, defect), lost)
    if result is None:
        result = not_verified_report(report, reason)
    await _leave(db, tenant_id, run, _observed(approval_message_id, p, result["balance"], defect), defect)
    return result
