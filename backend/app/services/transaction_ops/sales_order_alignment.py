"""Source-backed amendments to fully billed orders with a verified invoice discount.

Read/prepare/verify only. The existing signed approval dispatcher performs the
exact header update; this module never creates, rebills or reverses a document.
"""

import json
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

from app.services.transaction_ops.commercial_credits import _money, source_adjustment_basis
from app.services.transaction_ops.credit_classification import reference
from app.services.transaction_ops.netsuite_reader import _collection, _id, _sublist, authenticated_reader
from app.services.transaction_ops.sales_credit_profile import SalesCreditProfile

KIND = "sales_order_source_alignment"
MAX_LINES = 100
MAX_PROPOSAL_BYTES = 48_000
AMENDED_FIELDS = {"lastModifiedDate", "total", "discountItem", "discountRate", "discountTotal"}
FIELDS = (
    "id",
    "tranId",
    "status",
    "entity",
    "subsidiary",
    "currency",
    "exchangeRate",
    "tranDate",
    "lastModifiedDate",
    "subtotal",
    "total",
    "taxTotal",
    "taxRate",
    "taxItem",
    "shippingCost",
    "discountItem",
    "discountRate",
    "discountTotal",
    "billingSchedule",
    "createdFrom",
    "account",
    "postingPeriod",
    "class",
    "department",
    "location",
)
LINE_FIELDS = (
    "line",
    "orderLine",
    "item",
    "itemType",
    "quantity",
    "quantityBilled",
    "quantityFulfilled",
    "quantityBackOrdered",
    "amount",
    "rate",
    "isClosed",
    "location",
    "department",
    "class",
    "taxCode",
    "taxRate1",
    "taxAmount",
)


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if k != "links"}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return str(value) if isinstance(value, Decimal) else value


def snapshot(raw, *, amendable=False):
    from app.services.transaction_ops.state_service import business_digest

    problems = []
    lines = _sublist(raw, "item", "item", frozenset(LINE_FIELDS), problems)
    if problems or not lines or len(lines) > MAX_LINES:
        raise ValueError("Complete native lines within the amendment review limit are required.")
    protected = {k: v for k, v in raw.items() if k not in (AMENDED_FIELDS if amendable else set())}
    projected = {k: clean(raw.get(k)) for k in FIELDS}
    # A customer ID suffices for identity; do not retain contact details or
    # arbitrary native fields in a chat approval, group manifest or audit.
    projected["entity"] = {"id": reference(raw, "entity")} if raw.get("entity") else None
    return {**projected, "lines": clean(lines), "native_digest": business_digest(clean(protected))}


def linked_query(order_id, subsidiary):
    if not _id(order_id) or not _id(subsidiary):
        raise ValueError("Native linked-record scope is required.")
    return (
        "SELECT DISTINCT t.id, t.type, t.status, t.foreigntotal FROM transaction t "
        "JOIN transactionline tl ON tl.transaction=t.id "
        f"WHERE tl.createdfrom={order_id} AND t.subsidiary={subsidiary} "
        "AND t.type IN ('CustInvc','CashSale','ItemShip','CustCred','CustRfnd') ORDER BY t.id"
    )


async def read_support(db, tenant_id, review, invoice_id, order_id):
    scope = review["scope"]
    if not _id(invoice_id) or not _id(order_id):
        raise ValueError("Native record identities are required.")
    async with authenticated_reader(
        db, tenant_id, review["netsuite_connection_id"], scope["netsuite_account_id"], max_api_calls=5
    ) as reader:
        order = await reader.request("GET", f"/record/v1/salesOrder/{order_id}", params={"expandSubResources": "true"})
        invoice = await reader.request("GET", f"/record/v1/invoice/{invoice_id}", params={"expandSubResources": "true"})
        if str(order.get("id")) != str(order_id) or str(invoice.get("id")) != str(invoice_id):
            raise ValueError("Native record identity changed.")
        if any(reference(record, "subsidiary") != str(scope["subsidiary_id"]) for record in (order, invoice)):
            raise ValueError("Native subsidiary scope changed.")
        currency_id = reference(order, "currency")
        if not _id(currency_id):
            raise ValueError("Native currency identity is required.")
        currency = await reader.request("GET", f"/record/v1/currency/{currency_id}")
        linked = await reader.request(
            "POST",
            "/query/v1/suiteql",
            params={"limit": 100, "offset": 0},
            body={"q": linked_query(order_id, scope["subsidiary_id"])},
        )
        rows, complete = _collection(linked)
        if not complete:
            raise ValueError("Linked record coverage is incomplete.")
        # Pin a stable order snapshot after collecting dependent evidence.
        end = await reader.request("GET", f"/record/v1/salesOrder/{order_id}")
        if any(str(end.get(k)) != str(order.get(k)) for k in ("id", "lastModifiedDate", "total")):
            raise ValueError("Sales order changed during evidence collection.")
        return {
            "currency": {k: clean(currency.get(k)) for k in ("id", "symbol", "currencyPrecision")},
            "order": snapshot(order, amendable=True),
            "invoice": snapshot(invoice),
            "linked_documents": clean(rows),
            "invoice_amount_paid": str(invoice.get("amountPaid")),
            "invoice_amount_remaining": str(invoice.get("amountRemaining")),
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }


def build_candidate(tenant_id, case_id, source, review, evidence, support):
    """Fail closed outside this verified, zero-tax, fully fulfilled/billed treatment."""
    try:
        proof = evidence["commercial_credit_resolution"]
        basis = source_adjustment_basis(source)
        profile = SalesCreditProfile.model_validate(review["sales_credit_profile"])
        order, invoice = support["order"], support["invoice"]
        amount, total = _money(basis["credit_amount"]), _money(basis["source_total"])
        scope = review["scope"]
        if (
            proof["status"] != "existing_invoice_discount_verified"
            or review.get("configuration_status") != "scoped_configuration_found"
            or not review.get("connection_active")
            or not review.get("native_mcp_connector_id")
            or profile.account_id != scope["netsuite_account_id"]
            or profile.subsidiary_id != str(scope["subsidiary_id"])
            or str(proof["invoice_id"]) != str(invoice["id"])
            or str(proof["item_id"]) != profile.item_id
            or str(proof["sales_adjustment_account"]) != profile.adjustment_account_id
            or str(proof["ar_account"]) != profile.ar_account_id
            or str(proof["accounting_book"]) != profile.accounting_book_id
            or any(a["label"] != profile.source_adjustment_label for a in basis["adjustments"])
            or _money(source["payment_total"]) != 0
            or source["number"] != order["tranId"]
            or reference(invoice, "createdFrom") != str(order["id"])
            or (order.get("status") or {}).get("id") != "G"
            or (invoice.get("status") or {}).get("id") != "Open"
            or any(reference(order, k) != reference(invoice, k) for k in ("entity", "subsidiary", "currency"))
            or reference(order, "subsidiary") != profile.subsidiary_id
            or source["currency"] != profile.currency
            or str(support["currency"]["id"]) != reference(order, "currency")
            or support["currency"]["symbol"] != profile.currency
            or support["currency"]["currencyPrecision"] != 2
            or review["business_entity_subsidiaries"].get(source["business_entity"]) != profile.subsidiary_id
            or _money(order["exchangeRate"]) != 1
            or _money(invoice["exchangeRate"]) != 1
            or any(_money(d[k]) != 0 for d in (order, invoice) for k in ("taxTotal", "taxRate", "shippingCost"))
            or _money(basis["tax"]) != 0
            or _money(support["invoice_amount_paid"]) != 0
            or _money(support["invoice_amount_remaining"]) != total
            or _money(invoice["total"]) != total
            or _money(invoice["discountTotal"]) != -amount
            or reference(invoice, "discountItem") != profile.item_id
            or _money(invoice["discountRate"]) != -amount
            or order["discountItem"] is not None
            or _money(order["discountRate"] or 0) != 0
            or _money(order["discountTotal"]) != 0
            or order["billingSchedule"] is not None
            or _money(order["total"]) != _money(basis["gross_before_order_adjustments"])
            or _money(order["subtotal"]) != _money(invoice["subtotal"])
            or amount <= 0
            or _money(order["total"]) - amount != total
            or not order["lastModifiedDate"]
        ):
            return None
        linked = support["linked_documents"]
        invoices = [r for r in linked if r["type"] in {"CustInvc", "CashSale"}]
        if len(invoices) != 1 or str(invoices[0]["id"]) != str(invoice["id"]):
            return None
        if any(r["type"] not in {"CustInvc", "ItemShip"} for r in linked):
            return None
        order_lines = {str(l["line"]): l for l in order["lines"]}
        if len(order_lines) != len(order["lines"]) or len(invoice["lines"]) != len(order_lines):
            return None
        seen = set()
        for line in invoice["lines"]:
            key = str(line["orderLine"])
            other = order_lines[key]
            if key in seen or reference(line, "item") != reference(other, "item"):
                return None
            seen.add(key)
            if (
                any(_money(line[k]) != _money(other[k]) for k in ("quantity", "amount", "rate"))
                or (other.get("itemType") or {}).get("id") in {"Discount", "Markup", "Subtotal"}
                or _money(other["quantity"]) <= 0
                or _money(other["amount"]) < 0
                or any(_money(other[k]) != _money(other["quantity"]) for k in ("quantityBilled", "quantityFulfilled"))
                or _money(other["quantityBackOrdered"]) != 0
                or other.get("isClosed") is not False
            ):
                return None
        if sum((_money(l["amount"]) for l in order_lines.values()), Decimal(0)) != _money(order["subtotal"]):
            return None
        from app.services.transaction_ops.sales_credit import _native_number

        candidate = clean(
            deepcopy(
                {
                    "kind": KIND,
                    "tenant_id": str(tenant_id),
                    "case_id": str(case_id),
                    "order_reference": source["number"],
                    "record_type": "salesorder",
                    "lock_record_type": "invoice",
                    "record_id": str(order["id"]),
                    "mutation_type": "update",
                    "connector_id": str(review["native_mcp_connector_id"]),
                    "connection_id": str(review["netsuite_connection_id"]),
                    "config_id": str(review["config_id"]),
                    "scope": scope,
                    "profile": profile.model_dump(mode="json"),
                    "source": source,
                    "before": order,
                    "support": {k: v for k, v in support.items() if k != "order"},
                    "invoice_id": str(invoice["id"]),
                    "observed_at": support["observed_at"],
                    "accounting_book": profile.accounting_book_id,
                    "ar_account": profile.ar_account_id,
                    "sales_adjustment_account": profile.adjustment_account_id,
                    "proposed_fields": {
                        "discountItem": {"id": profile.item_id},
                        "discountRate": _native_number(-amount),
                    },
                    "expected_after": {
                        "total": str(total),
                        "subtotal": str(order["subtotal"]),
                        "taxTotal": "0.00",
                        "discountTotal": str(-amount),
                    },
                    "approval_basis": "Amend the sales order to reflect the finalized source adjustment "
                    "already verified on "
                    "its invoice. Apply only the displayed header discount. Retain items, quantities, fulfillment and "
                    "billing state, classifications and original date. This does not authorize rebilling, another "
                    "invoice/credit, a GL adjustment or cash movement. Verify the linked invoice and its ledger remain "
                    "unchanged, and retain before/after evidence, source version and original approver "
                    "in the audit log.",
                }
            )
        )
        if len(json.dumps(candidate, separators=(",", ":")).encode()) > MAX_PROPOSAL_BYTES:
            return None
        return candidate
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


async def prepare(db, tenant_id, case_id, source, review, evidence):
    proof = evidence.get("commercial_credit_resolution") or {}
    order = evidence.get("sections", {}).get("sales_order") or {}
    if proof.get("status") != "existing_invoice_discount_verified" or not review.get("sales_credit_profile"):
        return None
    if not _id(order.get("id")) or not _id(proof.get("invoice_id")):
        return None
    support = await read_support(db, tenant_id, review, proof["invoice_id"], order["id"])
    sections = evidence["sections"]
    observed = sections.get("posting_documents") or []
    if (
        len(observed) != 1
        or not observed[0].get("lastModifiedDate")
        or observed[0]["lastModifiedDate"] != support["invoice"]["lastModifiedDate"]
        or reference(observed[0], "account") != reference(support["invoice"], "account")
    ):
        raise ValueError("Invoice posting proof and native snapshot are not from the same observed version.")
    support["invoice_gl"] = clean(sections["gl"][str(proof["invoice_id"])])
    support["invoice_applications"] = clean(sections["invoice_applications"])
    support["discount_item"] = clean(sections["invoice_discount_item"])
    evidence["native_api_calls"] = evidence.get("native_api_calls", 0) + 5
    evidence["sales_order_alignment_support"] = {
        "order_id": str(support["order"]["id"]),
        "invoice_id": str(support["invoice"]["id"]),
        "order_total": support["order"]["total"],
        "invoice_total": support["invoice"]["total"],
        "order_line_count": len(support["order"]["lines"]),
        "observed_at": support["observed_at"],
        "snapshot_location": "Exact native snapshots are retained with the signed proposal and audit record.",
    }
    return build_candidate(tenant_id, case_id, source, review, evidence, support)


def is_update(record_type, normalized):
    return record_type.lower() == "salesorder" and any(
        str(k).lower() in {"discountitem", "discountrate"} for k in normalized.fields
    )


def require_binding(tenant_id, tool_name, record_type, normalized, p):
    from app.services.chat.tools import parse_external_tool_name

    parsed = parse_external_tool_name(tool_name)
    if (
        not p
        or p.get("kind") != KIND
        or p.get("tenant_id") != str(tenant_id)
        or not parsed
        or parsed[1] != "ns_updateRecord"
        or str(parsed[0]) != p.get("connector_id")
        or record_type.lower() != "salesorder"
        or normalized.record_id != p.get("record_id")
        or normalized.record != p.get("proposed_fields")
        or normalized.lines
    ):
        raise ValueError("A sales-order amendment requires its exact source-backed approval.")


def review_for_card(db, tenant_id, tool_name, record_type, normalized):
    p = db.info.get("accounting_correction_candidate")
    require_binding(tenant_id, tool_name, record_type, normalized, p)
    if not 0 <= (datetime.now(timezone.utc) - datetime.fromisoformat(p["observed_at"])).total_seconds() <= 300:
        raise ValueError("Refresh the sales-order amendment evidence.")
    return p


async def validate_approved(db, tenant_id, tool_name, tool_input, proposal):
    from urllib.parse import urlsplit
    from uuid import UUID

    from app.services.chat.write_payload import normalize_write_payload
    from app.services.mcp_connector_service import get_mcp_connector
    from app.services.transaction_ops.accounting_evidence import collect_accounting_evidence
    from app.services.transaction_ops.accounting_review import accounting_context
    from app.services.transaction_ops.case_service import get_case
    from app.services.transaction_ops.commercial_credits import collect_commercial_credits
    from app.services.transaction_ops.tax_correction import refresh_source

    p = proposal or {}
    require_binding(tenant_id, tool_name, tool_input.get("recordType", ""), normalize_write_payload(tool_input), p)
    connector = await get_mcp_connector(db, UUID(p["connector_id"]), tenant_id)
    if (
        not connector
        or connector.status != "active"
        or not connector.is_enabled
        or urlsplit(connector.server_url).hostname != f"{p['scope']['netsuite_account_id']}.suitetalk.api.netsuite.com"
    ):
        raise ValueError("The approved sales-order connector binding changed.")
    case = await get_case(db, tenant_id, UUID(p["case_id"]))
    review = await accounting_context(db, tenant_id, case.scope_json, case.latest_report_json)
    if str(review.get("config_id")) != p["config_id"] or review.get("sales_credit_profile") != p["profile"]:
        raise ValueError("Sales-order configuration changed; prepare a new approval.")
    source = await refresh_source(db, tenant_id, p["scope"], p["order_reference"])
    evidence = await collect_accounting_evidence(db, tenant_id, review, case.latest_report_json)
    await collect_commercial_credits(db, tenant_id, review, case.latest_report_json, source, evidence)
    fresh = await prepare(db, tenant_id, case.id, source, review, evidence)
    if not fresh or any(
        clean(fresh[k]) != clean(p[k])
        for k in ("source", "before", "proposed_fields", "expected_after", "scope", "invoice_id")
    ):
        raise ValueError("Sales order, source or invoice eligibility changed; prepare a new approval.")
    for key in (
        "invoice",
        "linked_documents",
        "invoice_amount_paid",
        "invoice_amount_remaining",
        "invoice_gl",
        "invoice_applications",
        "discount_item",
    ):
        if clean(fresh["support"][key]) != clean(p["support"][key]):
            raise ValueError("Linked invoice, billing or fulfillment evidence changed.")


async def verify_after(db, tenant_id, p, receipt=None):
    if isinstance(receipt, dict) and any(
        str(receipt[k]) != p["record_id"] for k in ("id", "recordId", "internalId") if receipt.get(k)
    ):
        return {"status": "needs_review", "reason": "sales_order_receipt_identity_conflict", "retry_allowed": False}
    review = {"scope": p["scope"], "netsuite_connection_id": p["connection_id"]}
    fresh = await read_support(db, tenant_id, review, p["invoice_id"], p["record_id"])
    order, before = fresh["order"], p["before"]
    unchanged = set(before) | set(order)
    unchanged -= AMENDED_FIELDS
    ok = all(clean(order.get(k)) == clean(before.get(k)) for k in unchanged)
    ok = ok and clean(order["lines"]) == clean(before["lines"])
    ok = ok and all(_money(order[k]) == _money(v) for k, v in p["expected_after"].items())
    ok = ok and reference(order, "discountItem") == p["profile"]["item_id"]
    ok = ok and _money(order["discountRate"]) == _money(str(p["proposed_fields"]["discountRate"]))
    ok = ok and all(
        clean(fresh[k]) == clean(p["support"][k])
        for k in ("invoice", "linked_documents", "invoice_amount_paid", "invoice_amount_remaining")
    )
    # Independently re-collect posted invoice, application and GL evidence. The
    # order amendment must not be used as proof that its invoice is correct.
    from uuid import UUID

    from app.services.transaction_ops.accounting_evidence import collect_accounting_evidence
    from app.services.transaction_ops.accounting_review import accounting_context
    from app.services.transaction_ops.case_service import get_case
    from app.services.transaction_ops.commercial_credits import collect_commercial_credits
    from app.services.transaction_ops.tax_correction import refresh_source

    case = await get_case(db, tenant_id, UUID(p["case_id"]))
    full_review = await accounting_context(db, tenant_id, case.scope_json, case.latest_report_json)
    source = await refresh_source(db, tenant_id, p["scope"], p["order_reference"])
    evidence = await collect_accounting_evidence(db, tenant_id, full_review, case.latest_report_json)
    await collect_commercial_credits(db, tenant_id, full_review, case.latest_report_json, source, evidence)
    proof = evidence.get("commercial_credit_resolution") or {}
    sections = evidence.get("sections") or {}
    ok = ok and clean(sections.get("gl", {}).get(p["invoice_id"])) == p["support"]["invoice_gl"]
    ok = ok and clean(sections.get("invoice_applications")) == p["support"]["invoice_applications"]
    ok = ok and clean(sections.get("invoice_discount_item")) == p["support"]["discount_item"]
    ok = (
        ok
        and clean(source) == p["source"]
        and proof.get("status") == "existing_invoice_discount_verified"
        and str(proof.get("invoice_id")) == p["invoice_id"]
    )
    return {
        "status": "verified" if ok else "needs_review",
        "sales_order": order,
        "invoice": fresh["invoice"],
        "invoice_resolution": proof,
        "linked_documents_unchanged": ok,
        "retry_allowed": False,
        "cash_settlement": "not_verified",
    }
