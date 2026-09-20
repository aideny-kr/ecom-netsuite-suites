#!/usr/bin/env python3
"""Measure Jev on the two Tier A call sites, using SYNTHETIC labelled cases only.

No tenant data is sent: every case below is invented, and the run allow-lists
one synthetic tenant id in-process. This answers "is Jev fast and right enough
on our question shapes?" It does NOT answer "does Jev agree with our accountants
on real exceptions?" — that needs shadow mode on real data, after zero-retention
terms (see the spec's risks section).

    export TYPESAFE_API_KEY=...            # from ~/.hermes/.env; never inline
    cd backend && PYTHONPATH=. .venv/bin/python ../scripts/jev_eval.py [--site preturn|recon|all] [--repeat 3]

Exit 0 = ran and met the spec's kill rule (p50 <= 400 ms and no unsafe
short-circuit); 1 = ran and missed it; 2 = could not run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys

SYNTHETIC_TENANT = "00000000-0000-0000-0000-00000000e7a1"
SOURCES = {
    "netsuite": "ERP ledger and transactions",
    "metabase": "BI dashboards",
    "bigquery": "data warehouse",
}

_ANALYSIS = [
    {"role": "user", "content": "How many orders shipped last week?"},
    {"role": "assistant", "content": "Here are last week's shipped orders by day."},
]
_CASE = [
    {
        "role": "user",
        "content": "Why doesn't invoice INV-10432 match its Stripe charge?",
    },
    {
        "role": "assistant",
        "content": "The invoice tax differs from the charge. I gathered the evidence for this case.",
    },
]

# (request, history, gold kind, gold continuation, user talks about data sources)
PRETURN = [
    ("How many sales orders did we ship last week?", [], "analytics", False, False),
    ("Show revenue by subsidiary for June 2026", [], "analytics", False, False),
    (
        "What are our top 10 customers by sales this quarter?",
        [],
        "analytics",
        False,
        False,
    ),
    ("List open purchase orders more than 30 days old", [], "analytics", False, False),
    (
        "Compare gross margin this month versus last month",
        [],
        "analytics",
        False,
        False,
    ),
    ("Inventory on hand by location", [], "analytics", False, False),
    ("total refunds by month this year", [], "analytics", False, False),
    ("average order value by sales platform", [], "analytics", False, False),
    ("Break those down by status", _ANALYSIS, "analytics", True, False),
    ("same thing but for the week before", _ANALYSIS, "analytics", True, False),
    ("Now count all Solidus orders", _CASE, "analytics", False, False),
    ("Pull the order counts from Metabase, not NetSuite", [], "analytics", False, True),
    ("Use BigQuery for this: sessions by day", [], "analytics", False, True),
    ("Which source should I use for order counts?", [], "analytics", False, True),
    (
        "Not NetSuite, anything else is fine. Show orders by status",
        [],
        "analytics",
        False,
        True,
    ),
    (
        "Compare what NetSuite and Metabase say for June revenue",
        [],
        "analytics",
        False,
        True,
    ),
    ("actually use the warehouse instead", _ANALYSIS, "analytics", True, True),
    (
        "Investigate why invoice INV-10432 doesn't match the Stripe charge",
        [],
        "transaction",
        False,
        False,
    ),
    ("Why is order R628489275 out of balance?", [], "transaction", False, False),
    ("Show me the evidence", _CASE, "transaction", True, False),
    ("fix it", _CASE, "transaction", True, False),
    ("retry the correction for that order", _CASE, "transaction", True, False),
    ("Retry the failed Celigo flow for Shopify orders", [], "operations", False, False),
    ("Set up a new NetSuite connection", [], "operations", False, False),
    (
        "Schedule the inventory aging report every Monday morning",
        [],
        "operations",
        False,
        False,
    ),
    ("Is the Shopify sync job healthy?", [], "operations", False, False),
    ("Update the memo on sales order SO-1182", [], "operations", False, False),
    ("thanks, that helps", _ANALYSIS, "conversation", True, False),
    ("ok got it", _CASE, "conversation", True, False),
    ("hi", [], "conversation", False, False),
    (
        "What does carry forward mean in reconciliation?",
        [],
        "conversation",
        False,
        False,
    ),
    ("Explain how SuiteQL handles dates", [], "conversation", False, False),
    ("what can you do?", [], "conversation", False, False),
]


def _recon(gold, **over):
    ctx = {
        "root_cause": "amount_mismatch", "planner_action": "needs_human",
        "planner_narrative": "The rule engine could not resolve this difference.",
        "proposed_amount": "3.20", "currency": "USD", "above_materiality": "False",
        "variance_type": "amount_mismatch", "variance_amount": "-3.20", "stripe_amount": "100.00",
        "netsuite_amount": "96.80", "variance_explanation": "", "evidence": {"order_reference": "R100200300"},
        "candidate_postings": [], "payout_line": None,
    }  # fmt: skip
    ctx.update(over)
    return gold, ctx


def _line(fee, description="Charge", line_type="charge"):
    return {
        "line_type": line_type,
        "amount": "100.00",
        "fee": fee,
        "net": "0",
        "currency": "USD",
        "description": description,
    }


_DEPOSIT = {"record_type": "customerdeposit", "amount": "100.00", "currency": "USD", "memo": "Order R100200300", "netsuite_internal_id": "9"}  # fmt: skip

RECON = [
    _recon("book_fee_line", payout_line=_line("3.20"), variance_explanation="NetSuite deposit is lower than the Stripe charge."),
    _recon("book_fee_line", payout_line=_line("3.20", "Charge for R100200300"), variance_explanation="Deposit booked net of processing fee."),
    _recon("book_fee_line", variance_amount="-2.90", netsuite_amount="97.10", payout_line=_line("3.20"), variance_explanation="Difference is close to the processor fee."),
    _recon("apply_deposit", variance_type="missing_in_netsuite", variance_amount="0.00", netsuite_amount="100.00", candidate_postings=[_DEPOSIT], evidence={"order_reference": "R100200300", "deposit_unapplied": "true"}, variance_explanation="A customer deposit exists for this order but is not applied to the invoice."),
    _recon("apply_deposit", variance_type="timing", variance_amount="0.00", netsuite_amount="100.00", candidate_postings=[_DEPOSIT], evidence={"order_reference": "R100200300", "deposit_status": "unapplied"}, variance_explanation="Deposit recorded, application to the sales order is missing."),
    _recon("create_and_apply_deposit", variance_type="missing_in_netsuite", variance_amount="100.00", netsuite_amount=None, evidence={"order_reference": "R100200300", "payout_status": "paid"}, variance_explanation="Charge settled in a paid payout three weeks ago. No deposit found in NetSuite."),
    _recon("create_and_apply_deposit", variance_type="missing_in_netsuite", variance_amount="100.00", netsuite_amount=None, evidence={"order_reference": "R100200300", "payout_status": "paid", "sales_order_found": "true"}, variance_explanation="Sales order exists; customer deposit was never created."),
    _recon("writeoff_je", variance_type="fx_rounding", variance_amount="-0.03", netsuite_amount="99.97", variance_explanation="Currency conversion rounding difference."),
    _recon("writeoff_je", variance_type="fx_rounding", variance_amount="0.02", netsuite_amount="100.02", variance_explanation="Rounding between EUR settlement and USD booking."),
    _recon("carry_forward", variance_type="missing_in_netsuite", variance_amount="100.00", netsuite_amount=None, evidence={"order_reference": "R100200300", "payout_status": "in_transit", "payout_age": "recent"}, variance_explanation="Payout created yesterday; NetSuite sync has not run yet."),
    _recon("carry_forward", variance_type="timing", variance_amount="0.00", netsuite_amount="100.00", evidence={"order_reference": "R100200300"}, variance_explanation="Amounts agree; the deposit date falls one day after the payout."),
    _recon("carry_forward", root_cause="washout", variance_type="missing_in_netsuite", variance_amount="100.00", netsuite_amount=None, evidence={"order_reference": "R100200300", "washout": "true"}, variance_explanation="A same-order refund cancels this charge out within the week."),
    _recon("needs_human", root_cause="chargeback", variance_type="chargeback", payout_line=_line("15.00", "Dispute fee", "dispute"), variance_explanation="Customer disputed the charge with their bank."),
    _recon("needs_human", variance_type="missing_in_netsuite", variance_amount="100.00", netsuite_amount=None, evidence={"order_reference": "R100200300", "payout_status": "failed"}, variance_explanation="The payout failed; funds never settled."),
    _recon("needs_human", variance_amount="-41.00", netsuite_amount="59.00", above_materiality="True", payout_line=_line("3.20"), variance_explanation="Large unexplained difference."),
    _recon("needs_human", variance_type="manual_adjustment", variance_amount="-12.00", netsuite_amount="88.00", evidence={}, variance_explanation="Manual journal touched this account; purpose unknown."),
]  # fmt: skip


def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))] if xs else None


async def eval_preturn(repeat: int) -> dict:
    from app.core.config import settings
    from app.services.chat import preturn_jev as pj
    from app.services.typesafe.client import ask

    rows, latencies, tokens = [], [], []
    for task, history, gold_kind, gold_cont, source_talk in PRETURN:
        for _ in range(repeat):
            result = await ask(
                SYNTHETIC_TENANT, *pj.build_request(task, history, SOURCES)
            )
            latencies.append(result.elapsed_ms)
            tokens.append(result.input_tokens)
        a = result.answers
        route = pj.to_route(a, floor=settings.JEV_ROUTE_MIN_CONFIDENCE)
        rows.append(
            {
                "task": task,
                "gold": gold_kind,
                "jev": a["kind"]["choice"],
                "conf": round(a["kind"]["confidence"], 2),
                "cont": round(a["continuation"]["noul"], 2),
                "gold_cont": gold_cont,
                "source_talk_p": round(a["source_talk"]["noul"], 2),
                "gold_source_talk": source_talk,
                "short_circuit": route is not None,
                "route_correct": None
                if route is None
                else (route.kind == gold_kind and route.continuation == gold_cont),
            }  # fmt: skip
        )
    fired = [r for r in rows if r["short_circuit"]]
    # The harmful error: Jev skips the LLM router on a request whose source choice the router had to read.
    unsafe = [r for r in fired if r["gold_source_talk"]]
    return {
        "site": "preturn", "cases": len(rows),
        "kind_accuracy": round(sum(r["gold"] == r["jev"] for r in rows) / len(rows), 3),
        "short_circuit_rate": round(len(fired) / len(rows), 3),
        "short_circuit_precision": round(sum(bool(r["route_correct"]) for r in fired) / len(fired), 3) if fired else None,
        "unsafe_short_circuits": len(unsafe),
        "latency_ms_p50": _pct(latencies, 0.5), "latency_ms_p90": _pct(latencies, 0.9),
        "mean_input_tokens": round(statistics.mean(tokens)),
        "usd_per_1000_turns": round(statistics.mean(tokens) * 0.042 / 1e6 * 1000, 4),
        "misses": [r for r in rows if r["gold"] != r["jev"] or r["route_correct"] is False or r in unsafe],
    }  # fmt: skip


async def eval_recon(repeat: int) -> dict:
    from app.core.config import settings
    from app.services.reconciliation import resolution_jev as rj
    from app.services.typesafe.client import ask

    rows, latencies = [], []
    for gold, context in RECON:
        for _ in range(repeat):
            result = await ask(SYNTHETIC_TENANT, *rj.build_request(context))
            latencies.append(result.elapsed_ms)
        a = result.answers["action"]
        rows.append({"gold": gold, "jev": a["choice"], "conf": round(a["confidence"], 2),
                     "explanation": context["variance_explanation"]})  # fmt: skip
    confident = [r for r in rows if r["conf"] >= settings.JEV_RECON_MIN_CONFIDENCE]
    return {
        "site": "recon", "cases": len(rows),
        "accuracy_all": round(sum(r["gold"] == r["jev"] for r in rows) / len(rows), 3),
        "coverage_at_floor": round(len(confident) / len(rows), 3),
        "accuracy_at_floor": round(sum(r["gold"] == r["jev"] for r in confident) / len(confident), 3) if confident else None,
        "latency_ms_p50": _pct(latencies, 0.5), "latency_ms_p90": _pct(latencies, 0.9),
        "misses": [r for r in rows if r["gold"] != r["jev"]],
    }  # fmt: skip


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", choices=["preturn", "recon", "all"], default="all")
    parser.add_argument(
        "--repeat",
        type=int,
        default=3,
        help="calls per case, for a steadier latency figure",
    )
    args = parser.parse_args()

    from app.core.config import settings
    from app.services.typesafe.client import JevUnavailableError

    if not settings.TYPESAFE_API_KEY:
        print("TYPESAFE_API_KEY is not set; nothing was sent.", file=sys.stderr)
        return 2
    settings.JEV_TENANT_ALLOWLIST = (
        SYNTHETIC_TENANT  # synthetic cases only; see module docstring
    )

    reports = []
    try:
        if args.site in ("preturn", "all"):
            reports.append(await eval_preturn(args.repeat))
        if args.site in ("recon", "all"):
            reports.append(await eval_recon(args.repeat))
    except JevUnavailableError as exc:
        print(f"Jev unavailable: {exc.reason}", file=sys.stderr)
        return 2

    print(json.dumps({"model": settings.JEV_MODEL, "reports": reports}, indent=2))
    fast = all((r["latency_ms_p50"] or 10**9) <= 400 for r in reports)
    safe = all(r.get("unsafe_short_circuits", 0) == 0 for r in reports)
    return 0 if fast and safe else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
