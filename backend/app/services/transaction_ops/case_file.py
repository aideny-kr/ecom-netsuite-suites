"""The resolver's first look at a case: one compact case file from saved evidence only.

Spec 2026-10-01 (accounting resolver), block B5. It answers "what do we already know
about this order?" without a NetSuite or Solidus read: the comparison, the saved
Solidus order, the sales order and its lines, the NetSuite documents earlier reads
saved, refunds and invoice credits, and verified fixes. Fresh reads stay with
`transaction_ops_accounting_evidence` and the live chain reader (B6); the file says
where to look next instead of guessing.

Readable names and amounts copied from the inputs. The only computed value is a line's
Solidus amount (price x quantity), to compare with NetSuite's line net; the only derived
facts are exact comparisons (an adjustment equal to the difference, a line whose
quantity or amount differs). The file is bounded (`MAX_CHARS`) so a huge order cannot
flood the context; anything cut is marked `truncated`.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import select

MAX_CHARS = 16_000  # ~4k tokens
MAX_LINES = 30
MAX_ADJUSTMENTS = 20
MAX_DOCUMENTS = 20
NETSUITE_SALES_ORDER = "https://{account}.app.netsuite.com/app/accounting/transactions/salesord.nl?id={id}"

_TYPES = {
    "CustInvc": "invoice",
    "invoice": "invoice",
    "CashSale": "cash sale",
    "cashsale": "cash sale",
    "CustCred": "credit memo",
    "creditmemo": "credit memo",
    "CustDep": "customer deposit",
    "DepAppl": "deposit application",
    "CustPymt": "customer payment",
    "CustRfnd": "customer refund",
    "CashRfnd": "cash refund",
    "RtnAuth": "return authorization",
    "ItemShip": "item fulfillment",
    "SalesOrd": "sales order",
}


def _amount(value):
    try:
        return None if value in (None, "") else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _ref(value):
    return value.get("refName") if isinstance(value, dict) else value


def _comparison(report):
    balance = report.get("balance") or {}
    metrics = {}
    for metric in ("order_total", "tax", "refunds"):
        row = (balance.get("amounts") or {}).get(metric) or {}
        delta = _amount(row.get("delta"))
        metrics[metric] = {
            "solidus": row.get("source"),
            "netsuite": row.get("target"),
            "difference": row.get("delta"),
            "differs": None if delta is None else delta != 0,
        }
    explained = [
        {"kind": a.get("kind"), "total": a.get("total")}
        for a in balance.get("adjustments") or []
        if isinstance(a, dict) and a.get("kind")
    ]
    return {
        "status": balance.get("status"),
        "reason": balance.get("reason"),
        "currency": balance.get("currency"),
        "metrics": metrics,
        **({"explained_by": explained} if explained else {}),
    }


def _order_adjustments(source):
    rows = []
    for a in source.get("adjustments") or []:
        if not isinstance(a, dict) or a.get("source_type") == "Spree::TaxRate":
            continue
        if a.get("adjustable_type") not in (None, "Spree::Order"):
            continue
        rows.append(
            {"label": a.get("label"), "amount": a.get("amount"), "date": (a.get("created_at") or "")[:10] or None}
        )
    return rows


def _solidus(source):
    if not isinstance(source, dict):
        return {"available": False, "reason": "no saved Solidus order for this case"}
    lines = []
    for li in source.get("line_items") or []:
        if not isinstance(li, dict):
            continue
        variant = li.get("variant") or {}
        promos = [
            {"label": a.get("label"), "amount": a.get("amount")}
            for a in li.get("adjustments") or []
            if isinstance(a, dict) and a.get("source_type") != "Spree::TaxRate"
        ]
        lines.append(
            {
                "id": li.get("id"),
                "product": variant.get("name"),
                "sku": variant.get("sku"),
                "qty": li.get("quantity"),
                "price": li.get("price"),
                "total": li.get("total"),
                **({"promotions": promos} if promos else {}),
            }
        )
    return {
        "available": True,
        "state": source.get("state"),
        "payment_state": source.get("payment_state"),
        "shipment_state": source.get("shipment_state"),
        "customer_type": source.get("customer_type"),
        "order_type": source.get("order_type"),
        "completed_at": source.get("completed_at"),
        "totals": {
            "items": source.get("item_total"),
            "shipping": source.get("ship_total"),
            "tax_included": source.get("included_tax_total"),
            "tax_added": source.get("additional_tax_total"),
            "adjustments": source.get("adjustment_total"),
            "total": source.get("total"),
            "paid": source.get("payment_total"),
        },
        "order_adjustments": _order_adjustments(source)[:MAX_ADJUSTMENTS],
        "payments": [
            {"amount": p.get("amount"), "state": p.get("state")}
            for p in source.get("payments") or []
            if isinstance(p, dict)
        ][:10],
        "lines": lines,
    }


def _line_differences(solidus, target):
    ns = {line.get("key"): line for line in target.get("lines") or [] if isinstance(line, dict) and line.get("key")}
    diffs = []
    for line in solidus.get("lines") or []:
        other = ns.get(f"line:{line.get('id')}")
        if not other:
            continue
        # NetSuite's net is the line amount (quantity x rate), so it compares with Solidus's
        # price x quantity, never the unit price (live 2026-10-02, R190994976).
        price, qty = _amount(line.get("price")), _amount(line.get("qty"))
        amount = price * qty if price is not None and qty is not None else None
        differs = []
        if _amount(other.get("quantity")) != qty:
            differs.append("quantity")
        if _amount(other.get("net")) is not None and amount is not None and _amount(other.get("net")) != amount:
            differs.append("amount")
        if differs:
            diffs.append(
                {
                    "product": line.get("product"),
                    "sku": line.get("sku"),
                    "solidus_qty": line.get("qty"),
                    "netsuite_qty": other.get("quantity"),
                    "solidus_amount": None if amount is None else str(amount),
                    "netsuite_net": other.get("net"),
                    "differs": differs,
                }
            )
    return diffs


def _documents(observation, report):
    """NetSuite documents earlier reads saved, once each, with readable fields only."""
    docs = {}
    sections = ((observation or {}).get("evidence") or {}).get("sections") or {}
    for row in (sections.get("linked_documents") or {}).get("rows") or []:
        if isinstance(row, dict) and row.get("id"):
            docs[str(row["id"])] = {
                "type": _TYPES.get(row.get("type"), row.get("type")),
                "number": row.get("tranid"),
                "id": str(row["id"]),
                "status": row.get("status_name"),
            }
    for doc in sections.get("posting_documents") or []:
        if not isinstance(doc, dict) or not doc.get("id"):
            continue
        entry = docs.setdefault(str(doc["id"]), {"id": str(doc["id"])})
        entry.update(
            {
                "type": _TYPES.get(doc.get("record_type"), entry.get("type") or doc.get("record_type")),
                "number": doc.get("tranId") or entry.get("number"),
                "status": _ref(doc.get("status")) or entry.get("status"),
                "total": doc.get("total"),
                "tax": doc.get("taxTotal"),
                "paid": doc.get("amountPaid"),
                "remaining": doc.get("amountRemaining"),
                "created_from": _ref(doc.get("createdFrom")),
                "period": _ref(doc.get("postingPeriod")),
            }
        )
    credits = ((report.get("refund_evidence") or {}).get("target") or {}).get("invoice_credits") or {}
    if credits.get("complete") is True:
        for credit in credits.get("credits") or []:
            if isinstance(credit, dict) and credit.get("id"):
                entry = docs.setdefault(str(credit["id"]), {"id": str(credit["id"])})
                entry.update(
                    {
                        "type": "credit memo",
                        "number": credit.get("number") or entry.get("number"),
                        "total": credit.get("total"),
                        "tax": credit.get("tax"),
                        "created_from_id": credit.get("invoice_id"),
                    }
                )
    return list(docs.values())


def _refunds(report):
    target = ((report.get("refund_evidence") or {}).get("target")) or {}
    if not target:
        return None
    credits = target.get("invoice_credits") or {}
    return {
        "netsuite_refunded": target.get("amount"),
        "refund_documents": target.get("refund_count"),
        "complete": target.get("complete"),
        "invoice_credits": (
            {"total": credits.get("total"), "tax": credits.get("tax"), "count": len(credits.get("credits") or [])}
            if credits.get("complete") is True
            else ({"unknown": credits.get("reason") or "not read"} if credits else None)
        ),
    }


def build_case_file(*, case, report, source_order, observation, corrections):
    """Pure: the same inputs always give the same file. No reads, no clock."""
    report = report or {}
    targets = report.get("targets") or []
    target = targets[0] if len(targets) == 1 and isinstance(targets[0], dict) else {}
    scope = case.get("scope") or {}
    solidus = _solidus(source_order)
    so_id = str(target.get("record_id") or "") or None
    sections = ((observation or {}).get("evidence") or {}).get("sections") or {}
    so_section = sections.get("sales_order") or {}
    difference = _amount(((report.get("balance") or {}).get("amounts") or {}).get("order_total", {}).get("delta"))
    explaining = [
        a["label"]
        for a in (solidus.get("order_adjustments") or [])
        if difference is not None and _amount(a.get("amount")) == difference
    ]
    account = str(scope.get("netsuite_account_id") or "").replace("_", "-").lower()
    result = {
        "case": {
            "id": str(case.get("id")),
            "order": case.get("order_reference"),
            "status": case.get("status"),
            "subsidiary_id": scope.get("subsidiary_id"),
            "last_observed_at": case.get("last_observed_at"),
        },
        "comparison": _comparison(report),
        "facts": {"adjustments_equal_to_difference": explaining},
        "solidus": solidus,
        "netsuite": {
            "sales_order": {
                "id": so_id,
                "number": so_section.get("tranId"),
                "status": target.get("status"),
                "subtotal": target.get("subtotal"),
                "discount": target.get("discount"),
                "shipping": target.get("shipping"),
                "tax": target.get("tax"),
                "total": target.get("total"),
                "link": NETSUITE_SALES_ORDER.format(account=account, id=so_id) if so_id and account else None,
            },
            "line_differences": _line_differences(solidus, target) if solidus.get("available") else [],
            "documents": _documents(observation, report)[:MAX_DOCUMENTS],
            "refunds": _refunds(report),
        },
        "history": {
            "verified_fixes": [{"record_type": t, "record_id": r} for t, r in sorted(corrections or [])],
        },
        "read_more": {
            "saved_observation": (
                {
                    "observation_id": observation.get("audit_id"),
                    "observed_at": observation.get("observed_at"),
                    "sections": sorted(sections),
                    "how": "transaction_ops_accounting_evidence(case_id, observation_id, section): saved, no new reads",
                }
                if observation
                else None
            ),
            "live": (
                "chain_read(record) or transaction_ops_accounting_evidence(case_id) for current NetSuite and Solidus"
            ),
        },
        "truncated": False,
    }
    return _bounded(result)


def _bounded(result):
    """Trim the longest lists until the file fits; say so."""
    if len(json.dumps(result, default=str)) <= MAX_CHARS:
        return result
    result["truncated"] = True
    for path, floor in (
        (("solidus", "lines"), 0),
        (("netsuite", "line_differences"), 5),
        (("netsuite", "documents"), 5),
        (("solidus", "order_adjustments"), 5),
    ):
        holder = result.get(path[0]) or {}
        items = holder.get(path[1])
        if not isinstance(items, list):
            continue
        cut = min(len(items), MAX_LINES)
        while cut > floor and len(json.dumps(result, default=str)) > MAX_CHARS:
            cut = max(floor, cut - 5 if cut > 10 else cut - 1)
            holder[path[1]] = items[:cut]
        if len(json.dumps(result, default=str)) <= MAX_CHARS:
            return result
        holder[path[1]] = items[:floor]
    return result


async def open_case(db, tenant_id, *, case_id=None, order_reference=None):
    """Load a case's saved evidence, tenant-scoped, and build its file. Never reads NetSuite or Solidus."""
    from app.core.database import set_tenant_context
    from app.models.audit import AuditEvent
    from app.models.transaction_ops import TransactionCase
    from app.services.transaction_ops import group_breakdown
    from app.services.transaction_ops.state_service import StateError

    if (case_id is None) == (order_reference is None):
        raise StateError("case_id_or_order_reference_required", 422)
    await set_tenant_context(db, str(tenant_id))
    query = select(TransactionCase).where(TransactionCase.tenant_id == tenant_id)
    if case_id is not None:
        try:
            query = query.where(TransactionCase.id == UUID(str(case_id)))
        except ValueError:
            raise StateError("invalid_case_id", 422) from None
    else:
        query = query.where(TransactionCase.order_reference == str(order_reference))
    cases = (await db.execute(query.order_by(TransactionCase.last_observed_at.desc()).limit(2))).scalars().all()
    if not cases:
        raise StateError("case_not_found", 404)
    if len(cases) > 1:
        raise StateError("order_has_several_cases_use_case_id", 409)
    case = cases[0]
    scope = case.scope_json or {}
    snapshots, _ = await group_breakdown._saved_source_orders(db, tenant_id, scope.get("source_connection_id"), [case])
    event = await db.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.action == "accounting.evidence.observed",
            AuditEvent.resource_type == "transaction_case",
            AuditEvent.resource_id == str(case.id),
        )
        .order_by(AuditEvent.timestamp.desc(), AuditEvent.id.desc())
        .limit(1)
    )
    observation = (
        {
            "audit_id": str(event.id),
            "observed_at": event.timestamp.isoformat(),
            "evidence": (event.payload or {}).get("evidence"),
        }
        if event
        else None
    )
    corrected = await group_breakdown._verified_corrections(db, tenant_id, [case.id])
    return build_case_file(
        case={
            "id": str(case.id),
            "order_reference": case.order_reference,
            "status": case.status,
            "scope": scope,
            "last_observed_at": case.last_observed_at.isoformat() if case.last_observed_at else None,
        },
        report=case.latest_report_json or {},
        source_order=group_breakdown._source_order(snapshots.get(case.order_reference), case.order_reference),
        observation=observation,
        corrections=corrected.get(str(case.id), set()),
    )
