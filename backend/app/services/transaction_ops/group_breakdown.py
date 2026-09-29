"""Break an issue group into causes before anyone tries to fix it.

case_groups groups open cases by symptom (the direction of each difference) and says so: a
group is never a verified cause. Running the group fix on a mixed group prepares nothing. The
46-order "Order differences" group of 2026-09-28 was four situations, none of them a NetSuite
correction the app can make, and group preparation set every member aside twice.

This reads what is already saved: each member's reconciliation report, its saved Solidus order
(adjustments, customer type) and the app's own verified corrections. Only for members that
evidence leaves open does it make at most two read-only NetSuite queries, the invoices created
from their sales orders, under a fixed call budget and deadline. Every member lands in exactly
one cause, and a cause is assigned only when its rule holds for that member. Anything else is
"no_shared_cause", never a guess. Read-only: nothing is proposed or changed here.

Amounts belong to the card the server renders. The model receives causes, counts and next steps
only (condensed_for_model), so it cannot misstate a figure.
"""

import asyncio
import re
import time
from collections import Counter
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import select

from app.core.database import set_tenant_context
from app.models.chat import ChatMessage
from app.models.transaction_ops import TransactionCase, TransactionConfig
from app.models.transaction_source_snapshot import TransactionSourceSnapshot
from app.services.transaction_ops.case_groups import _pattern, preparation_members
from app.services.transaction_ops.state_service import StateError, current_config_clause

METRICS = ("order_total", "tax", "refunds")
NETSUITE_SECONDS = 40  # the tool's own limit is 60 s; the database part takes seconds
NETSUITE_CALLS = 2
MIN_SETTING_EVIDENCE = 3  # a settings change affects every future refund with that reason
_ID = re.compile(r"[0-9]{1,20}\Z")

# One fixed vocabulary, so the model never writes a cause or a next step itself.
CAUSES = {
    "matched_now": {
        "label": "Matched on the latest check",
        "why": "The latest reconciliation of these orders matched; the group listing predates it.",
        "next_step": "none",
        "next_label": "Nothing to do. They leave the group when it refreshes.",
    },
    "corrected_in_app": {
        "label": "Already corrected here, still shown as open",
        "why": "Each order has an approved correction that the app verified in NetSuite, "
        "but the reconciliation does not recognise it yet.",
        "next_step": "recheck",
        "next_label": "Recheck these orders.",
    },
    "tax_left_after_credit": {
        "label": "Refund credit left the tax in place",
        "why": "A credit memo refunded the order, but NetSuite still carries the tax: the difference is all tax "
        "and no tax reversal was recognised. This is the shape the credit reallocation corrects.",
        "next_step": "prepare_corrections",
        "next_label": "Run the group fix. It prepares only the orders it can prove and sets the rest aside.",
    },
    "invoice_matches_source": {
        "label": "Invoice already matches Solidus",
        "why": "The invoice NetSuite posted equals the Solidus total. Only the sales order differs, "
        "so the books are right and nothing needs correcting.",
        "next_step": "reconciliation_rule",
        "next_label": "Compare these orders to the invoice instead of the sales order.",
    },
    "source_adjustment_not_in_netsuite": {
        "label": "Solidus adjustment never reached NetSuite",
        "why": "Each order has a manual Solidus adjustment equal to the difference to the cent. "
        "The order sync did not carry it to NetSuite.",
        "next_step": "fix_at_source",
        "next_label": "The order sync must carry Solidus order adjustments; new orders are probably affected too.",
    },
    "business_priced_in_netsuite": {
        "label": "Business orders priced in NetSuite",
        "why": "Business customers with no Solidus adjustment that explains the difference. "
        "NetSuite carries its own price.",
        "next_step": "needs_policy",
        "next_label": "Finance decides which system sets the price for business orders.",
    },
    "refund_without_credit_memo": {
        "label": "Refund request without a credit memo",
        "why": "A refund request exists but no credit memo is linked to it, so there is nothing to recognise yet.",
        "next_step": "review_individually",
        "next_label": "Review one by one.",
    },
    "no_shared_cause": {
        "label": "No shared cause",
        "why": "No rule explains these orders.",
        "next_step": "review_individually",
        "next_label": "Review one by one.",
    },
}
_ORDER = {key: index for index, key in enumerate(CAUSES)}
# The rules after the invoice check: an invoice could still overturn these. Earlier rules never read NetSuite.
_NEEDS_INVOICE = frozenset(
    {
        "source_adjustment_not_in_netsuite",
        "business_priced_in_netsuite",
        "refund_without_credit_memo",
        "no_shared_cause",
    }
)


def _decimal(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def _money(total):
    return str(total.quantize(Decimal("0.01")))


def _source_order(snapshot, reference):
    """The saved Solidus order for this reference, or None. Never another order's detail."""
    try:
        orders = snapshot.evidence_json["evidence"]["orders"]
    except (AttributeError, KeyError, TypeError):
        return None
    if not isinstance(orders, list) or len(orders) != 1 or not isinstance(orders[0], dict):
        return None
    return orders[0] if orders[0].get("number") == reference else None


def _order_adjustments(order):
    """Manual order-level adjustments: promotions, price corrections, "Adjusted to zero".

    Tax rates are computed tax, not an adjustment someone made, so they never explain a gap.
    """
    rows = []
    for entry in (order or {}).get("adjustments") or []:
        if (
            isinstance(entry, dict)
            and entry.get("adjustable_type") == "Spree::Order"
            and entry.get("source_type") != "Spree::TaxRate"
            and _decimal(entry.get("amount")) is not None
        ):
            rows.append(entry)
    return rows


def _explaining_adjustments(order, delta):
    """Adjustments that account for the whole order-total difference, exactly."""
    if not delta:
        return []
    rows = _order_adjustments(order)
    exact = [entry for entry in rows if _decimal(entry["amount"]) == delta]
    if exact:
        return exact[:1]
    if rows and sum((_decimal(entry["amount"]) for entry in rows), Decimal(0)) == delta:
        return rows
    return []


def _label(entry):
    text = " ".join(str(entry.get("label") or "").split())
    return (text[:80] + "…") if len(text) > 80 else (text or "Unlabelled adjustment")


async def _scope_config(db, tenant_id, scope):
    """The current investigation config for this scope: the refund settings and the connection."""
    if not isinstance(scope, dict):
        return None
    query = select(TransactionConfig).where(
        TransactionConfig.tenant_id == tenant_id,
        current_config_clause(),
        TransactionConfig.subsidiary_id == str(scope.get("subsidiary_id")),
        TransactionConfig.netsuite_account_id == str(scope.get("netsuite_account_id")),
        TransactionConfig.record_type == str(scope.get("record_type") or "salesorder"),
    )
    configs = [
        config
        for config in (await db.execute(query)).scalars()
        if str(config.source_connection_id or "") == str(scope.get("source_connection_id") or "")
        and str(config.source_step_id or "") == str(scope.get("source_step_id") or "")
    ]
    return configs[0] if len(configs) == 1 else None


async def _verified_corrections(db, tenant_id, case_ids):
    """Cases with an approved correction that the app itself verified in NetSuite."""
    review = ChatMessage.structured_output["accounting_review"]
    rows = await db.execute(
        select(review["case_id"].as_string()).where(
            ChatMessage.tenant_id == tenant_id,
            ChatMessage.role == "assistant",
            review["case_id"].as_string().in_([str(case_id) for case_id in case_ids]),
            ChatMessage.structured_output["status"].as_string() == "approved",
            ChatMessage.structured_output["accounting_verification"]["status"].as_string() == "verified",
        )
    )
    return {value for (value,) in rows}


async def _invoices(db, tenant_id, config, order_ids):
    """Invoice totals per sales order: two batched read-only queries, or None if incomplete."""
    from app.services.transaction_ops.netsuite_bulk import query
    from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError, authenticated_reader

    ids = sorted({value for value in order_ids if _ID.fullmatch(value)})
    if not ids or len(ids) > 500 or not config.netsuite_connection_id:
        return None, "not_needed" if not ids else "unavailable"
    try:
        async with asyncio.timeout(NETSUITE_SECONDS):
            async with authenticated_reader(
                db,
                tenant_id,
                config.netsuite_connection_id,
                config.netsuite_account_id,
                max_api_calls=NETSUITE_CALLS,
            ) as reader:
                # Bounded by id: an unbounded createdfrom join fails on this connector.
                links = await query(
                    reader,
                    "SELECT DISTINCT tl.transaction AS invoice_id, tl.createdfrom AS order_id "
                    "FROM transactionline tl JOIN transaction t ON t.id = tl.transaction "
                    f"WHERE t.type = 'CustInvc' AND tl.createdfrom IN ({','.join(ids)})",
                )
                invoice_ids = sorted(
                    {str(row.get("invoice_id")) for row in links if _ID.fullmatch(str(row.get("invoice_id")))}
                )
                totals = {}
                if invoice_ids:
                    rows = await query(
                        reader,
                        "SELECT t.id, t.foreigntotal, t.foreignamountunpaid, BUILTIN.DF(t.entity) AS customer "
                        f"FROM transaction t WHERE t.type = 'CustInvc' AND t.id IN ({','.join(invoice_ids)})",
                    )
                    totals = {str(row.get("id")): row for row in rows}
    except TimeoutError:
        return None, "timed_out"
    except (NetSuiteEvidenceError, StateError, KeyError, TypeError, ValueError):
        return None, "unavailable"
    by_order = {}
    for row in links:
        order_id, invoice_id = str(row.get("order_id")), str(row.get("invoice_id"))
        invoice = totals.get(invoice_id)
        total, unpaid = (
            _decimal((invoice or {}).get("foreigntotal")),
            _decimal((invoice or {}).get("foreignamountunpaid")),
        )
        if invoice is None or total is None or unpaid is None:
            return None, "unavailable"  # never classify on a partial invoice set
        entry = by_order.setdefault(order_id, {"total": Decimal(0), "open": Decimal(0), "customers": set()})
        entry["total"] += total
        entry["open"] += unpaid
        if invoice.get("customer"):
            entry["customers"].add(str(invoice["customer"])[:80])
    return by_order, "complete"


def _classify(member, invoices, tax_reasons):
    report, delta = member["report"], member["amounts"]["order_total"]
    if member["status"] != "open" or (report.get("balance") or {}).get("status") == "matched":
        return "matched_now", {}
    if member["corrected"]:
        reasons = sorted({link.get("reason_id") for link in member["links"] if link.get("reason_id")})
        return "corrected_in_app", {
            "refund_reasons": reasons,
            "counted_as_tax_refund": [r for r in reasons if r in tax_reasons],
        }
    tax = member["amounts"]["tax"]
    target = (report.get("refund_evidence") or {}).get("target") or {}
    if (
        tax
        and delta == tax
        and any(link.get("credit_memo_id") for link in member["links"])
        and not target.get("tax_adjustments")
    ):
        return "tax_left_after_credit", {}
    invoice = (invoices or {}).get(member["order_id"])
    source_total = _decimal(((report.get("balance") or {}).get("amounts") or {}).get("order_total", {}).get("source"))
    if delta and invoice is not None and source_total is not None and invoice["total"] == source_total:
        return "invoice_matches_source", {}
    explaining = _explaining_adjustments(member["order"], delta)
    if explaining:
        return "source_adjustment_not_in_netsuite", {"labels": [_label(entry) for entry in explaining]}
    if delta and not tax and (member["order"] or {}).get("customer_type") == "business":
        return "business_priced_in_netsuite", {}
    if member["links"] and not any(link.get("credit_memo_id") for link in member["links"]):
        return "refund_without_credit_memo", {}
    return "no_shared_cause", {}


def _facts(key, members, detail, invoices):
    counts = []
    if key == "corrected_in_app":
        reasons = detail["refund_reasons"]
        counts.append({"fact": "refund reason " + (", ".join(reasons) or "none"), "orders": len(members)})
        if reasons and not detail["counted_as_tax_refund"]:
            counts.append({"fact": "not counted as a tax refund in this subsidiary's settings", "orders": len(members)})
    if key == "source_adjustment_not_in_netsuite":
        labels = Counter(label for m in members for label in m["detail"]["labels"])
        counts.extend({"fact": f'"{label}"', "orders": n} for label, n in labels.most_common(5))
        if len(labels) > 5:
            counts.append(
                {
                    "fact": f"{len(labels) - 5} other adjustment labels",
                    "orders": sum(n for _, n in labels.most_common()[5:]),
                }
            )
    if key in ("invoice_matches_source", "source_adjustment_not_in_netsuite", "business_priced_in_netsuite"):
        types = Counter((m["order"] or {}).get("customer_type") or "unknown" for m in members)
        counts.extend({"fact": f"{kind} customer", "orders": n} for kind, n in types.most_common())
    if invoices is not None and key in ("invoice_matches_source", "business_priced_in_netsuite"):
        customers = Counter(c for m in members for c in (invoices.get(m["order_id"]) or {}).get("customers", ()))
        counts.extend({"fact": name, "orders": n} for name, n in customers.most_common(3) if n > 1)
    if key == "no_shared_cause":
        missing = sum(1 for m in members if m["order"] is None)
        if missing:
            counts.append({"fact": "no saved Solidus detail yet", "orders": missing})
    return counts


async def breakdown(db, tenant_id, *, group_id=None, case_id=None, review_run_ids=None, status=None, search=""):
    started = time.monotonic()
    await set_tenant_context(db, str(tenant_id))
    if (group_id is None) == (case_id is None):
        raise StateError("group_id_or_case_id_required", 422)
    if group_id is not None:
        scope = {
            key: value
            for key, value in (("review_run_ids", review_run_ids), ("status", status), ("search", search))
            if value
        }
        members = await preparation_members(db, tenant_id, group_id, **scope)
        case_ids = [UUID(str(m["case_id"])) for m in members]
    else:
        try:
            case_ids = [UUID(str(case_id))]
        except (ValueError, TypeError):
            raise StateError("invalid_case_id", 422) from None
    cases = (
        (
            await db.execute(
                select(TransactionCase).where(TransactionCase.tenant_id == tenant_id, TransactionCase.id.in_(case_ids))
            )
        )
        .scalars()
        .all()
    )
    if not cases or len(cases) != len(case_ids):
        raise StateError("Group is empty or changed; refresh the exact scoped group.", 422)
    scopes = {repr(sorted((case.scope_json or {}).items())) for case in cases}
    if len(scopes) != 1:
        raise StateError("group_scope_mixed", 422)
    scope_json = cases[0].scope_json or {}
    config = await _scope_config(db, tenant_id, scope_json)
    profile = ((config.mapping_json or {}).get("refund_adjustments") or {}) if config else {}
    tax_reasons = {str(r) for r in profile.get("tax_reversal_reason_ids") or []}

    snapshots = {}
    source_connection = scope_json.get("source_connection_id")
    if source_connection:
        rows = await db.execute(
            select(TransactionSourceSnapshot).where(
                TransactionSourceSnapshot.tenant_id == tenant_id,
                TransactionSourceSnapshot.connection_id == UUID(str(source_connection)),
                TransactionSourceSnapshot.order_reference.in_([case.order_reference for case in cases]),
            )
        )
        snapshots = {row.order_reference: row for row in rows.scalars()}
    corrected = await _verified_corrections(db, tenant_id, case_ids)

    members = []
    for case in cases:
        report = case.latest_report_json or {}
        amounts = {
            metric: _decimal((((report.get("balance") or {}).get("amounts") or {}).get(metric) or {}).get("delta"))
            or Decimal(0)
            for metric in METRICS
        }
        targets = report.get("targets") or []
        members.append(
            {
                "case_id": str(case.id),
                "reference": case.order_reference,
                "status": case.status,
                "report": report,
                "amounts": amounts,
                "order_id": str(
                    (targets[0] if len(targets) == 1 and isinstance(targets[0], dict) else {}).get("record_id") or ""
                ),
                "links": [
                    link
                    for link in (((report.get("refund_evidence") or {}).get("target") or {}).get("request_links") or [])
                    if isinstance(link, dict)
                ],
                "order": _source_order(snapshots.get(case.order_reference), case.order_reference),
                "corrected": str(case.id) in corrected,
            }
        )

    # NetSuite only for members the saved evidence leaves open, and only when it can decide something.
    open_ids = [
        m["order_id"]
        for m in members
        if m["amounts"]["order_total"] and _classify(m, {}, tax_reasons)[0] in _NEEDS_INVOICE
    ]
    if not open_ids:
        invoices, netsuite = None, "not_needed"
    elif config is None:
        invoices, netsuite = None, "unavailable"
    else:
        invoices, netsuite = await _invoices(db, tenant_id, config, open_ids)

    rows = {}
    for member in members:
        key, detail = _classify(member, invoices, tax_reasons)
        member["detail"] = detail
        group_key = (key, tuple(detail.get("refund_reasons", ())))
        rows.setdefault(group_key, {"key": key, "detail": detail, "members": []})["members"].append(member)

    causes = []
    for (key, _), row in sorted(
        rows.items(), key=lambda item: (item[0][0] == "no_shared_cause", -len(item[1]["members"]), _ORDER[item[0][0]])
    ):
        vocabulary = dict(CAUSES[key])
        if key == "corrected_in_app":
            reasons, counted = row["detail"]["refund_reasons"], row["detail"]["counted_as_tax_refund"]
            if reasons and not counted and len(row["members"]) >= MIN_SETTING_EVIDENCE:
                vocabulary["next_step"] = "settings_change"
                vocabulary["next_label"] = (
                    f"Count refund reason {', '.join(reasons)} as a tax refund for this subsidiary. "
                    "A person approves it."
                )
        amounts = {
            metric: _money(sum((m["amounts"][metric] for m in row["members"]), Decimal(0))) for metric in METRICS
        }
        if invoices is not None:
            opened = [invoices[m["order_id"]]["open"] for m in row["members"] if m["order_id"] in invoices]
            if len(opened) == len(row["members"]):
                amounts["open_on_invoices"] = _money(sum(opened, Decimal(0)))
        causes.append(
            {
                "cause": key,
                **vocabulary,
                "orders": len(row["members"]),
                "order_references": sorted(m["reference"] for m in row["members"]),
                "amounts": amounts,
                "facts": _facts(key, row["members"], row["detail"], invoices),
            }
        )
    balance = members[0]["report"].get("balance") or {}
    pattern_row = {
        "status": balance.get("status"),
        "currency": balance.get("currency"),
        "target_currency": balance.get("target_currency"),
        "missing_metrics": balance.get("missing_metrics") or [],
        **{
            kind: any(isinstance(a, dict) and a.get("kind") == kind for a in balance.get("adjustments") or [])
            for kind in ("tax_reversal", "credit_memo")
        },
        **{
            m: "zero" if not members[0]["amounts"][m] else ("negative" if members[0]["amounts"][m] < 0 else "positive")
            for m in METRICS
        },
    }
    return {
        "success": True,
        "group_id": group_id,
        "case_id": str(case_id) if case_id is not None else None,
        "pattern": _pattern(pattern_row) if group_id is not None else None,
        "currency": balance.get("currency"),
        "orders": len(members),
        "totals": {metric: _money(sum((m["amounts"][metric] for m in members), Decimal(0))) for metric in METRICS},
        "causes": causes,
        "checked": {
            "saved_evidence": len(members),
            "saved_source_orders": sum(1 for m in members if m["order"] is not None),
            "netsuite": netsuite,
            "netsuite_orders": len(open_ids) if netsuite == "complete" else 0,
            "seconds": round(time.monotonic() - started, 1),
        },
        "cause_rule": "A cause is shown only when its rule held for every order in it.",
    }


def condensed_for_model(result):
    """What the model reads: causes, counts, facts and next steps. No amounts."""
    return {
        "success": True,
        "shown_to_user": "The breakdown card is on screen with every amount. Do not restate amounts or totals.",
        "orders": result["orders"],
        "netsuite_check": result["checked"]["netsuite"],
        "causes": [
            {
                "cause": cause["cause"],
                "label": cause["label"],
                "orders": cause["orders"],
                "next_step": cause["next_step"],
                "facts": [f["fact"] for f in cause["facts"]],
            }
            for cause in result["causes"]
        ],
    }
