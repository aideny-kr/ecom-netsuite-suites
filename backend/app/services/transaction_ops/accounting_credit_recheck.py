"""Read-only, approval-bound invoice-minus-credit reconciliation.

Raw order evidence is retained. Only a proved existing-credit correction with
protected original records can use this posting basis; cash is never certified.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.services.transaction_ops import credit_api_correction
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.native_accounting_service import _stable
from app.services.transaction_ops.posting_balance import repriced_credit_balance

READ_CALLS = 56  # Accounting (20), support (24), source and OAuth maintenance allowance.


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
    for value in [support, evidence, *report["refund_evidence"].values()]:
        observed = datetime.fromisoformat(value["observed_at"])
        if not verified_at <= observed <= now or now - observed > timedelta(minutes=15):
            raise ValueError("credit_recheck_stale_evidence")
    # The full fresh order and posting records must describe this same revision.
    for metric, key in (("order_total", "total"), ("tax", "tax_total")):
        if Decimal(report["balance"]["amounts"][metric]["source"]) != Decimal(source[key]):
            raise ValueError("credit_recheck_source_changed")
    target_refund = report["refund_evidence"]["target"]
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


async def reconcile(db, tenant_id, run, p, report):
    now = datetime.now(timezone.utc)
    reason = "credit_recheck_evidence_unavailable"
    result = None
    remaining = (run.deadline_at - now).total_seconds()
    if remaining <= 0 or run.api_calls_used + READ_CALLS > run.max_api_calls:
        reason = "credit_recheck_budget_exhausted"
    else:
        # Reserve before reading; a failure also consumes its reserved budget.
        run.api_calls_used += READ_CALLS
        await state._audit(db, tenant_id, "accounting_recheck.read_budget", run, payload={"api_calls": READ_CALLS})
        lease_token = run.lease_token
        await state._commit(db, tenant_id)
        try:
            async with asyncio.timeout(min(90, remaining)):
                current = await credit_api_correction.fresh(db, tenant_id, p)
            result = project(
                p,
                report,
                current,
                verified_at=datetime.fromisoformat(run.params_json["verified_at"]),
                now=datetime.now(timezone.utc),
            )
        except (ValueError, KeyError, TypeError, ArithmeticError, TimeoutError):
            # Never reuse the approval-time snapshot or raw order balance as a
            # successful posting recheck after incomplete fresh evidence.
            pass
        # Reservation releases the database lock during provider I/O. Recheck
        # ownership before publishing evidence or changing the case verdict.
        run = await state.get_run(db, tenant_id, run.id, lock=True)
        state._lease(run, lease_token, datetime.now(timezone.utc))
    if result is None:
        result = {
            **report,
            "balance": {**report["balance"], "status": "not_verified", "reason": reason},
            "evidence_limits": {"code": reason},
        }
    await state._audit(
        db,
        tenant_id,
        "accounting_recheck.posting_observed",
        run,
        payload={
            "approval_message_id": run.params_json["approval_message_id"],
            "case_id": p["case_id"],
            "financial_writes": 0,
            "balance": result["balance"],
        },
    )
    return result
