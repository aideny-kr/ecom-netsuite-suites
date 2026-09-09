"""Bounded investigation routes from stored comparisons, never write instructions.

These hints do not change reconciliation, manufacture proposals, or authorize
execution. Native lifecycle, GL, tax and payment evidence still need fresh reads.
"""

from decimal import localcontext

from app.schemas.transaction_ops import _decimal


def investigation_guidance(report):
    balance = report.get("balance") or {}
    status = balance.get("status")
    result = {
        "kind": "investigation_guidance",
        "executable": False,
        "evidence_basis": "stored_observation_requires_refresh",
        "routes": [],
        "execution_requirements": (
            "Refresh scoped source and NetSuite evidence; prepare an exact supported proposal; "
            "obtain a new human approval; persist audit before execution and record approver, "
            "before/after and receipt; independently verify order total, tax and refunds. "
            "Payment or payout clearance requires separate evidence."
        ),
    }

    def route(code, instruction):
        result["routes"].append({"code": code, "next_step": instruction})

    if status in {"ambiguous", "currency_mismatch"}:
        route("establish_order_identity", "Verify exact order, account, subsidiary, currency and linked records.")
        return result
    if status == "missing_in_netsuite":
        route(
            "investigate_missing_order",
            "Refresh the complete native order search and inspect Celigo sync state "
            "before preparing a missing-order proposal.",
        )
        return result

    deltas = {}
    try:
        if status not in {"matched", "difference"} or balance.get("missing_metrics"):
            raise ValueError("incomplete")
        if not balance.get("currency") or balance.get("currency") != balance.get("target_currency"):
            raise ValueError("currency_unproven")
        for key in ("order_total", "tax", "refunds"):
            values = balance["amounts"][key]
            source, target, delta = (_decimal(values[name]) for name in ("source", "target", "delta"))
            with localcontext() as context:
                context.prec = 50
                if source - target != delta:
                    raise ValueError("inconsistent_amounts")
            deltas[key] = delta
        if (status == "matched") != (not any(deltas.values())):
            raise ValueError("inconsistent_status")
    except (KeyError, TypeError, ValueError, ArithmeticError):
        route(
            "collect_comparison_evidence",
            "Refresh missing or inconsistent comparison evidence; unknown amounts are not zero.",
        )
        return result

    if status == "matched":
        route(
            "no_financial_change",
            "Amounts reconcile in this observation; do not propose a financial correction from it.",
        )
        return result

    targets = report.get("targets") or []
    if len(targets) == 1 and targets[0].get("status") == "fulfilled":
        route(
            "inspect_posted_documents",
            "Inspect native billing status, invoices, credit memos, applications, GL impact and posting period. "
            "Do not edit a billed sales order to force a match; "
            "a posted adjustment needs an approved accounting treatment.",
        )
    else:
        route(
            "verify_native_lifecycle",
            "Verify current billing, fulfillment and posting state before choosing a correction adapter.",
        )
    if deltas["refunds"]:
        route(
            "reconcile_refund_chain",
            "Trace source completed refunds through refund requests, credit memos and native refund applications; "
            "check duplicates and processor evidence. "
            "A missing accounting record does not authorize issuing cash again.",
        )
    if deltas["tax"]:
        credits = any(
            isinstance(item, dict) and item.get("kind") in {"tax_reversal", "credit_memo"}
            for item in balance.get("adjustments") or []
        )
        route(
            "investigate_tax_after_credit" if credits else "investigate_tax_allocation",
            "Inspect finalized source tax and native line/shipping tax allocations, rounding and tax-account postings. "
            + (
                "The comparison already includes linked credits; investigate the remaining tax difference "
                "without crediting the same amount again."
                if credits
                else "Keep penny differences open; establish which allocation differs before proposing a change."
            ),
        )
    if deltas["order_total"]:
        route(
            "investigate_order_total",
            "Compare line, shipping, discount and linked-credit evidence. Gross already includes tax; "
            "do not add the tax variance to the gross variance when sizing a correction.",
        )
    return result
