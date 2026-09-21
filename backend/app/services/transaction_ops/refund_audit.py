"""Read-only Solidus audit evidence for review of an existing refund.

Audit timestamps find candidates; they do not establish refund allocation or
tax authority. Exact identities and independent current/native arithmetic must
corroborate one candidate, which still requires Finance's explicit approval.
"""

import json
import re
from datetime import datetime
from decimal import Decimal, localcontext

from app.services.transaction_ops import refund_reader
from app.services.transaction_ops.source_reader import SourceReadError

MAX_EVENTS = 100
FIELDS = (
    "total",
    "item_total",
    "additional_tax_total",
    "included_tax_total",
    "shipment_total",
    "promo_total",
    "adjustment_total",
    "price",
    "quantity",
)


def audit_query(order_reference, refund_id):
    if (
        not isinstance(order_reference, str)
        or not refund_reader._REFERENCE.fullmatch(order_reference)
        or not isinstance(refund_id, str)
        or not re.fullmatch(r"[0-9]{1,30}", refund_id)
    ):
        raise SourceReadError("invalid_refund_audit_identity", 422)
    keys = ",".join(f"'{key}'" for key in FIELDS)
    # Only whitelisted monetary columns enter the result. Never expose the
    # PaperTrail object wholesale: it can contain addresses and customer data.
    return f"""WITH target AS (
 SELECT r.id::text AS refund_id, r.amount::text AS gross, r.state AS refund_state,
 r.refund_reason_id, r.reimbursement_id, NULLIF(r.transaction_id,'') IS NOT NULL AS processor_reference_present,
 r.created_at AS refund_created_at, p.number AS payment_number,
 o.id::text AS order_id, o.number AS order_reference, o.currency,
 (SELECT COUNT(*) FROM spree_refunds rr JOIN spree_payments pp ON pp.id=rr.payment_id
  WHERE pp.order_id=o.id) AS order_refund_count
 FROM spree_refunds r JOIN spree_payments p ON p.id=r.payment_id
 JOIN spree_orders o ON o.id=p.order_id
 WHERE o.number='{order_reference}' AND r.id={refund_id}
), candidates AS (
 SELECT v.id::text AS version_id, v.item_type, v.item_id::text AS item_id,
 v.event, v.created_at,
 v.object->>'quantity' AS previous_quantity, v.object->>'order_id' AS previous_order_id,
 (SELECT jsonb_object_agg(k,value) FROM jsonb_each(v.object_changes) e(k,value)
  WHERE k IN ({keys})) AS changes
 FROM target t CROSS JOIN LATERAL (
   SELECT v.* FROM versions v
   WHERE v.item_type='Spree::Order' AND v.item_id=t.order_id::bigint
   AND v.created_at BETWEEN t.refund_created_at-INTERVAL '5 minutes'
                        AND t.refund_created_at+INTERVAL '5 minutes'
   AND v.object_changes ?| ARRAY[{keys}]
   UNION ALL
   SELECT v.* FROM spree_line_items li JOIN versions v
     ON v.item_type='Spree::LineItem' AND v.item_id=li.id
   WHERE li.order_id=t.order_id::bigint
   AND v.created_at BETWEEN t.refund_created_at-INTERVAL '5 minutes'
                        AND t.refund_created_at+INTERVAL '5 minutes'
   AND v.object_changes ?| ARRAY[{keys}]
 ) v
 ORDER BY v.created_at,v.id LIMIT {MAX_EVENTS + 1}
)
SELECT t.refund_id,t.gross,t.refund_state,t.refund_reason_id,t.reimbursement_id,
 t.processor_reference_present,t.refund_created_at::text AS refund_created_at,
 t.payment_number,t.order_id,t.order_reference,t.currency,t.order_refund_count,
 (SELECT COALESCE(jsonb_agg(to_jsonb(c) ORDER BY c.created_at,c.version_id::bigint),'[]'::jsonb)
 FROM candidates c) AS events FROM target t LIMIT 2
"""


async def read_audit(db, tenant_id, step_id, order_reference, refund_id):
    rows, step, connection = await refund_reader._read_rows(
        db, tenant_id, step_id, audit_query(order_reference, refund_id), 2
    )
    if len(rows) != 1:
        raise SourceReadError("refund_audit_identity_unproven")
    row = dict(rows[0])
    if isinstance(row.get("events"), str):
        try:
            row["events"] = json.loads(row["events"])
        except ValueError:
            raise SourceReadError("refund_audit_invalid_events") from None
    return {"source_step_id": str(step.id), "connection_id": str(connection.id), "row": row}


def money(value):
    if isinstance(value, (bool, float)) or value is None:
        raise ValueError("invalid_audit_money")
    result = Decimal(str(value))
    if not result.is_finite() or abs(result) > Decimal("1e15") or result != result.quantize(Decimal(".01")):
        raise ValueError("invalid_audit_money")
    return result


def pair(changes, key):
    values = changes[key]
    if not isinstance(values, list) or len(values) != 2:
        raise ValueError("invalid_audit_pair")
    return tuple(money(value) for value in values)


def review_allocation(audit, source, invoice, basis, line_changes, link):
    """Fail closed on missing, ambiguous or contradictory history.

    The result is evidence inside the signed proposal, never a posting rule.
    Re-collection at execution compares the full support envelope, including
    versions, connector and exact amounts, with the approved envelope.
    """
    unavailable = {"status": "needs_review", "reason": "refund_audit_incomplete_or_ambiguous"}
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            row = audit["row"]
            gross, net, tax = (-money(basis[k]) for k in ("gross_delta", "net_delta", "tax_delta"))
            if (
                str(row["order_id"]) != str(source["id"])
                or row["order_reference"] != source["number"]
                or row["currency"] != source["currency"]
                or str(row["refund_id"]) != str(link["source_refund_id"])
                or row["payment_number"] != link["payment_number"]
                or not row["payment_number"]
                or str(row["refund_state"]) != "2"
                or str(row["refund_reason_id"]) != "38"
                or row["reimbursement_id"] is not None
                or row["processor_reference_present"] is not True
                or str(row["order_refund_count"]) != "1"
                or money(row["gross"]) != gross
                or money(link["amount"]) != gross
                or gross != net + tax
                or min(net, tax) <= 0
            ):
                return {**unavailable, "reason": "refund_audit_identity_or_amount_mismatch"}
            events = row["events"]
            if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS:
                return unavailable
            ids = [e["version_id"] for e in events]
            if len(set(ids)) != len(ids) or any(not re.fullmatch(r"[0-9]{1,30}", str(i)) for i in ids):
                return unavailable
            # The query uses database timestamps for its window; compare all
            # values at microsecond precision in the returned JSON as well.
            refund_at = datetime.fromisoformat(row["refund_created_at"].replace("Z", "+00:00")).replace(tzinfo=None)
            for event in events:
                at = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00")).replace(tzinfo=None)
                if event["event"] != "update" or not 0 <= (refund_at - at).total_seconds() <= 300:
                    return unavailable
                changes = event["changes"]
                allowed = (
                    {"total", "item_total", "additional_tax_total", "adjustment_total"}
                    if (event["item_type"] == "Spree::Order")
                    else {"price", "additional_tax_total", "adjustment_total"}
                )
                if not changes or not set(changes).issubset(allowed):
                    return unavailable
                if any(key in changes for key in ("quantity", "included_tax_total", "shipment_total", "promo_total")):
                    return unavailable
                if "adjustment_total" in changes:
                    if pair(changes, "adjustment_total") != pair(changes, "additional_tax_total"):
                        return unavailable
            orders = [e for e in events if e["item_type"] == "Spree::Order"]
            if len(orders) != 1 or str(orders[0]["item_id"]) != str(source["id"]):
                return unavailable
            header = orders[0]["changes"]
            for key, source_key, native_key in (
                ("total", "total", "total"),
                ("item_total", "item_total", "subtotal"),
                ("additional_tax_total", "tax_total", "taxTotal"),
            ):
                if pair(header, key) != (money(invoice[native_key]), money(source[source_key])):
                    return {**unavailable, "reason": "refund_audit_header_changed"}
            by_line = {}
            for event in events:
                if event["item_type"] == "Spree::Order":
                    continue
                if event["item_type"] != "Spree::LineItem" or str(event["previous_order_id"]) != str(source["id"]):
                    return unavailable
                by_line.setdefault(str(event["item_id"]), []).append(event)
            changes_by_id = {str(c["source_line_id"]): c for c in line_changes}
            if not by_line or set(by_line) != set(changes_by_id) or len(changes_by_id) != len(line_changes):
                return unavailable
            lines, sum_net, sum_tax = [], Decimal(0), Decimal(0)
            for identifier, history in sorted(by_line.items()):
                change = changes_by_id[identifier]
                quantities = {money(e["previous_quantity"]) for e in history}
                qty = money(change["source_quantity"])
                if quantities != {qty} or qty <= 0 or qty != money(change["target_quantity"]):
                    return unavailable
                prices = [e for e in history if "price" in e["changes"]]
                taxes = [e for e in history if "additional_tax_total" in e["changes"]]
                if len(prices) != 1 or len(taxes) != 1:
                    return unavailable
                old_price, new_price = pair(prices[0]["changes"], "price")
                old_tax, new_tax = pair(taxes[0]["changes"], "additional_tax_total")
                observation = change["tax_observation"]
                if (
                    (old_price, new_price) != (money(change["target_unit_price"]), money(change["source_unit_price"]))
                    or (old_tax, new_tax)
                    != (money(observation["target_custom_vat_amount"]), money(observation["source_adjustment_amount"]))
                    or old_price <= new_price
                    or new_price < 0
                    or old_tax <= new_tax
                    or new_tax < 0
                ):
                    return {**unavailable, "reason": "refund_audit_line_changed"}
                line_net, line_tax = (old_price - new_price) * qty, old_tax - new_tax
                sum_net += line_net
                sum_tax += line_tax
                lines.append(
                    {
                        "source_line_id": identifier,
                        "sku": change["sku"],
                        "quantity": str(qty),
                        "price_before": str(old_price),
                        "price_after": str(new_price),
                        "tax_before": str(old_tax),
                        "tax_after": str(new_tax),
                        "net": str(line_net),
                        "tax": str(line_tax),
                        "gross": str(line_net + line_tax),
                        "version_ids": [str(e["version_id"]) for e in history],
                    }
                )
            if sum_net != net or sum_tax != tax:
                return unavailable
            return {
                "status": "ready_for_finance_review",
                "source": "solidus_audit_history",
                "source_step_id": audit["source_step_id"],
                "connection_id": audit["connection_id"],
                "order_id": str(row["order_id"]),
                "order_reference": row["order_reference"],
                "source_refund_id": str(row["refund_id"]),
                "payment_number": row["payment_number"],
                "currency": row["currency"],
                "net": str(net),
                "tax": str(tax),
                "gross": str(gross),
                "order_version_id": str(orders[0]["version_id"]),
                "lines": lines,
                "authority": "Audit history corroborates the amounts; it has no explicit refund-to-line link. "
                "Finance must confirm that these changes belong to this refund and approve the tax treatment.",
            }
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
        return unavailable
