"""The resolver's first look at a case: one compact case file from saved evidence only.

Spec 2026-10-01 (accounting resolver), block B5. It answers "what do we already know
about this order?" without a NetSuite or Solidus read: the comparison, the saved
Solidus order, the sales order and its lines, the NetSuite documents earlier reads
saved, refunds and invoice credits, and verified fixes. Fresh reads stay with
`transaction_ops_accounting_evidence` and the live chain reader (B6); the file says
where to look next instead of guessing.

Readable names and amounts copied from the inputs. The only computed value is a line's
Solidus amount (price x quantity) for comparison with NetSuite's line net, and only when
nothing is included in the price (no included tax, no promotions); otherwise the line
says why its amount is not compared. The only derived facts are exact comparisons (an adjustment equal
to the difference, a line whose quantity or amount differs). The file is bounded
(`MAX_CHARS`, checked last) so no order can flood the context; any cut sets `truncated`.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import select

MAX_CHARS = 16_000  # ~4k tokens
MAX_LINES = 30
MAX_ADJUSTMENTS = 20
MAX_PAYMENTS = 10
MAX_DOCUMENTS = 20
MAX_FACTS = 5
MAX_FIXES = 10
MAX_TEXT = 120
NETSUITE_SALES_ORDER = "https://{account}.app.netsuite.com/app/accounting/transactions/salesord.nl?id={id}"
# The saved-observation reader (group_investigation.read_observation) accepts only these.
OBSERVATION_SECTIONS = ("source", "documents", "applications", "assessment")

_TYPES = {
    "CustInvc": "invoice",
    "invoice": "invoice",
    "CashSale": "cash sale",
    "cashsale": "cash sale",
    "CustCred": "credit memo",
    "creditmemo": "credit memo",
    "CustDep": "customer deposit",
    "customerdeposit": "customer deposit",
    "DepAppl": "deposit application",
    "depositapplication": "deposit application",
    "CustPymt": "customer payment",
    "customerpayment": "customer payment",
    "CustRfnd": "customer refund",
    "customerrefund": "customer refund",
    "CashRfnd": "cash refund",
    "cashrefund": "cash refund",
    "RtnAuth": "return authorization",
    "returnauthorization": "return authorization",
    "ItemShip": "item fulfillment",
    "itemfulfillment": "item fulfillment",
    "SalesOrd": "sales order",
    "salesorder": "sales order",
}


class _Cuts:
    """Records whether any list or text was shortened, so `truncated` is never silently false."""

    def __init__(self):
        self.any = False

    def take(self, items, limit):
        items = list(items)
        if len(items) > limit:
            self.any = True
        return items[:limit]

    def text(self, value, limit=MAX_TEXT):
        if isinstance(value, str) and len(value) > limit:
            self.any = True
            return value[: limit - 1] + "…"
        return value


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


def _order_adjustments(source, cuts):
    rows = []
    for a in source.get("adjustments") or []:
        if not isinstance(a, dict) or a.get("source_type") == "Spree::TaxRate":
            continue
        if a.get("adjustable_type") not in (None, "Spree::Order"):
            continue
        rows.append(
            {
                "label": cuts.text(a.get("label")),
                "amount": a.get("amount"),
                "date": (a.get("created_at") or "")[:10] or None,
            }
        )
    return rows


def _comparable_net(li, price, qty, order_includes_tax):
    """Solidus net to compare with NetSuite's line net, or (None, reason).

    Compared only when nothing is included in the price: no included tax on the order or
    the line, and no promotions. Anything else would mean inferring tax or discounts, and a
    wrong inference becomes a false fact (review rounds 1 and 2 on PR B5); quantities are
    still compared.
    """
    adjustments = [a for a in li.get("adjustments") or [] if isinstance(a, dict)]
    if any(a.get("source_type") != "Spree::TaxRate" for a in adjustments):
        return None, "promotions on the line"
    line_included = _amount(li.get("included_tax_total"))
    if order_includes_tax or (line_included or Decimal(0)) != 0 or any(a.get("included") is True for a in adjustments):
        return None, "tax included in prices"
    if price is None or qty is None:
        return None, "price or quantity missing"
    return price * qty, None


def _solidus(source, cuts):
    if not isinstance(source, dict):
        return {"available": False, "reason": "no saved Solidus order for this case"}
    order_includes_tax = (_amount(source.get("included_tax_total")) or Decimal(0)) != 0
    lines = []
    for li in source.get("line_items") or []:
        if not isinstance(li, dict):
            continue
        variant = li.get("variant") or {}
        price, qty = _amount(li.get("price")), _amount(li.get("quantity"))
        promos = [
            {"label": cuts.text(a.get("label")), "amount": a.get("amount")}
            for a in li.get("adjustments") or []
            if isinstance(a, dict) and a.get("source_type") != "Spree::TaxRate"
        ]
        net, not_compared = _comparable_net(li, price, qty, order_includes_tax)
        lines.append(
            {
                "id": li.get("id"),
                "product": cuts.text(variant.get("name")),
                "sku": variant.get("sku"),
                "qty": li.get("quantity"),
                "price": li.get("price"),
                "total": li.get("total"),
                **({"promotions": promos} if promos else {}),
                **({"net_for_comparison": str(net)} if net is not None else {"amount_not_compared": not_compared}),
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
        "order_adjustments": cuts.take(_order_adjustments(source, cuts), MAX_ADJUSTMENTS),
        "payments": cuts.take(
            (
                {"amount": p.get("amount"), "state": p.get("state")}
                for p in source.get("payments") or []
                if isinstance(p, dict)
            ),
            MAX_PAYMENTS,
        ),
        "lines": lines,
    }


def _line_differences(solidus, target):
    ns = {line.get("key"): line for line in target.get("lines") or [] if isinstance(line, dict) and line.get("key")}
    diffs = []
    for line in solidus.get("lines") or []:
        other = ns.get(f"line:{line.get('id')}")
        if not other:
            continue
        # NetSuite's net is the line amount (quantity x rate, before tax), so it compares with
        # the Solidus line's net, never the unit price (live 2026-10-02, R190994976).
        net = _amount(line.get("net_for_comparison"))
        differs = []
        if _amount(other.get("quantity")) != _amount(line.get("qty")):
            differs.append("quantity")
        if _amount(other.get("net")) is not None and net is not None and _amount(other.get("net")) != net:
            differs.append("amount")
        if differs:
            diffs.append(
                {
                    "product": line.get("product"),
                    "sku": line.get("sku"),
                    "solidus_qty": line.get("qty"),
                    "netsuite_qty": other.get("quantity"),
                    "solidus_amount": line.get("net_for_comparison"),
                    "netsuite_net": other.get("net"),
                    "differs": differs,
                }
            )
    return diffs


def _merge_document(docs, doc, cuts):
    if not isinstance(doc, dict) or not doc.get("id"):
        return
    entry = docs.setdefault(str(doc["id"]), {"id": str(doc["id"])})
    fields = {
        "type": _TYPES.get(doc.get("record_type"), entry.get("type") or doc.get("record_type")),
        "number": doc.get("tranId") or entry.get("number"),
        "status": _ref(doc.get("status")) or entry.get("status"),
        "total": doc.get("total"),
        "tax": doc.get("taxTotal"),
        "paid": doc.get("amountPaid"),
        "remaining": doc.get("amountRemaining"),
        "created_from": cuts.text(_ref(doc.get("createdFrom"))),
        "period": _ref(doc.get("postingPeriod")),
    }
    entry.update({k: v for k, v in fields.items() if v is not None or k not in entry})


def _documents(observation, report, cuts):
    """NetSuite documents earlier reads saved, once each, readable fields only.

    The same saved sections `record_links.evidence_record_links` uses, plus the linked
    documents inventory and the refund read's invoice credits.
    """
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
        _merge_document(docs, doc, cuts)
    for doc in sections.get("deposits") or []:
        _merge_document(docs, doc, cuts)
    for doc in ((sections.get("invoice_applications") or {}).get("documents") or {}).values():
        _merge_document(docs, doc, cuts)
    for doc in (sections.get("related_refund_documents") or {}).get("documents") or []:
        _merge_document(docs, doc, cuts)
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
    cuts = _Cuts()
    report = report or {}
    targets = report.get("targets") or []
    target = targets[0] if len(targets) == 1 and isinstance(targets[0], dict) else {}
    scope = case.get("scope") or {}
    solidus = _solidus(source_order, cuts)
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
        "facts": {"adjustments_equal_to_difference": cuts.take(explaining, MAX_FACTS)},
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
            "documents": cuts.take(_documents(observation, report, cuts), MAX_DOCUMENTS),
            "refunds": _refunds(report),
        },
        "history": {
            "verified_fixes": cuts.take(
                ({"record_type": t, "record_id": r} for t, r in sorted(corrections or [])), MAX_FIXES
            ),
        },
        "read_more": {
            "saved_observation": (
                {
                    "observation_id": observation.get("audit_id"),
                    "observed_at": observation.get("observed_at"),
                    "sections": list(OBSERVATION_SECTIONS),
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
    if solidus.get("available"):
        solidus["lines"] = cuts.take(solidus["lines"], MAX_LINES)
    result["truncated"] = cuts.any
    return _bounded(result)


def _size(result):
    return len(json.dumps(result, default=str))


def _bounded(result):
    """Shorten lists, then texts, until the file fits. The final check always holds."""
    if _size(result) <= MAX_CHARS:
        return result
    result["truncated"] = True
    solidus, netsuite = result.get("solidus") or {}, result.get("netsuite") or {}
    for holder, key, floor in (
        (solidus, "lines", 0),
        (netsuite, "line_differences", 5),
        (netsuite, "documents", 5),
        (solidus, "order_adjustments", 5),
        (solidus, "payments", 3),
        (result.get("history") or {}, "verified_fixes", 3),
        (result.get("facts") or {}, "adjustments_equal_to_difference", 1),
    ):
        items = holder.get(key)
        while isinstance(items, list) and len(items) > floor and _size(result) > MAX_CHARS:
            items = items[: max(floor, len(items) - max(1, len(items) // 4))]
            holder[key] = items
        if _size(result) <= MAX_CHARS:
            return result
    for adjustment in solidus.get("order_adjustments") or []:
        if isinstance(adjustment.get("label"), str) and len(adjustment["label"]) > 40:
            adjustment["label"] = adjustment["label"][:39] + "…"
    facts = result.get("facts") or {}
    facts["adjustments_equal_to_difference"] = [
        (label[:39] + "…") if isinstance(label, str) and len(label) > 40 else label
        for label in facts.get("adjustments_equal_to_difference") or []
    ]
    if _size(result) <= MAX_CHARS:
        return result
    # The bound by construction: clip every string and list, tighter each pass, then a fixed
    # file. Patching one oversized field at a time failed twice (review rounds 1 and 2).
    for text_limit, list_limit in ((200, 10), (80, 5), (40, 3), (20, 1)):
        clipped = _clip(result, text_limit, list_limit)
        if _size(clipped) <= MAX_CHARS:
            return clipped
    return {"truncated": True, "note": "The saved evidence for this case is too large to summarise; open it by id."}


def _clip(value, text_limit, list_limit):
    if isinstance(value, str):
        return value if len(value) <= text_limit else value[: text_limit - 1] + "…"
    if isinstance(value, list):
        return [_clip(item, text_limit, list_limit) for item in value[:list_limit]]
    if isinstance(value, dict):
        return {key: _clip(item, text_limit, list_limit) for key, item in value.items()}
    return value


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
