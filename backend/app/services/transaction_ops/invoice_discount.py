"""Exact, source-backed discounts on fully unpaid invoices; never write here."""

import json
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

from app.services.transaction_ops.commercial_credits import _money
from app.services.transaction_ops.credit_classification import FIELDS, reference
from app.services.transaction_ops.netsuite_reader import _collection, _id

KIND = "invoice_sales_adjustment"


def verified_existing_discount(basis, invoice, applications, gl, item):
    """Recognize a posted invoice discount from complete native ledger evidence.

    This read-only recipe is deliberately limited to fully unpaid, zero-tax
    invoices. Equal gross amounts alone never prove a valid posting.
    """
    try:
        amount, total = _money(basis["credit_amount"]), _money(basis["source_total"])
        if (
            applications.get("complete") is not True
            or applications["links"]
            or applications["documents"]
            or gl.get("complete") is not True
            or not gl["rows"]
            or any(_money(invoice[k]) != 0 for k in ("amountPaid", "taxTotal", "taxRate", "shippingCost"))
            or _money(basis["tax"]) != 0
            or _money(invoice["total"]) != total
            or _money(invoice["amountRemaining"]) != total
            or _money(invoice["subtotal"]) != _money(basis["item_total"])
            or _money(invoice["discountRate"]) != -amount
            or _money(invoice["discountTotal"]) != -amount
            or _money(invoice["exchangeRate"]) != 1
            or reference(invoice, "discountItem") != str(item["id"])
            or item.get("isInactive") is not False
            or item.get("nonPosting") is not False
        ):
            return None
        subsidiary_rows, complete = _collection(item.get("subsidiary") or {})
        if not complete or reference(invoice, "subsidiary") not in {_id(r.get("id")) for r in subsidiary_rows}:
            return None
        ar, offset = reference(invoice, "account"), reference(item, "account")
        if not ar or not offset or ar == offset:
            return None
        rows = [r for r in gl["rows"] if _money(r.get("debit") or 0) or _money(r.get("credit") or 0)]
        books = {str(r["accountingbook"]) for r in rows}
        if len(books) != 1:
            return None
        ar_rows = [r for r in rows if str(r["account"]) == ar]
        discounts = [r for r in rows if str(r["account"]) == offset]
        if (
            len(ar_rows) != 1
            or _money(ar_rows[0].get("debit") or 0) != total
            or _money(ar_rows[0].get("credit") or 0)
            or len(discounts) != 1
            or _money(discounts[0].get("debit") or 0) != amount
            or _money(discounts[0].get("credit") or 0)
            or any(
                sum((_money(r.get(side) or 0) for r in rows), Decimal(0)) != total + amount
                for side in ("debit", "credit")
            )
            or any(_money(r.get("debit") or 0) for r in rows if str(r["account"]) not in {ar, offset})
        ):
            return None
        return {
            "status": "existing_invoice_discount_verified",
            "invoice_id": str(invoice["id"]),
            "discount_amount": str(amount),
            "tax_amount": "0.00",
            "net_invoice_total": str(total),
            "remaining_variance": "0.00",
            "invoice_remaining": str(total),
            "sales_adjustment_account": offset,
            "ar_account": ar,
            "accounting_book": next(iter(books)),
            "item_id": str(item["id"]),
            "bank_processor_clearance": "not_verified",
            "next_action": "Recognize the verified invoice discount; do not discount or credit it again. "
            "Refunds and cash settlement remain separate checks.",
        }
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


def item_usable(item, profile):
    rows, complete = _collection(item.get("subsidiary") or {})
    return (
        str(item.get("id")) == profile.item_id
        and item.get("isInactive") is False
        and item.get("nonPosting") is False
        and reference(item, "account") == profile.adjustment_account_id
        and complete
        and profile.subsidiary_id in {_id(r.get("id")) for r in rows}
    )


def from_commercial_candidate(candidate):
    """Use the existing scoped commercial evidence, but never credit an unpaid invoice.

    Initial supported discount treatment is zero tax, no existing discounts,
    one open/current invoice period and no payments/applications. Taxable,
    locked-period and ambiguous documents require their own treatment.
    """
    from app.services.transaction_ops.sales_credit import _native_number
    from app.services.transaction_ops.sales_credit_profile import SalesCreditProfile

    try:
        p = deepcopy(candidate)
        before, support = p["before"], p["support"]
        profile = SalesCreditProfile.model_validate(p["profile"])
        lines = support.get("invoice_lines")
        if (
            _money(before["amountPaid"]) != 0
            or _money(before["amountRemaining"]) != _money(before["total"])
            or _money(p["source"]["payment_total"]) != 0
            or (before.get("status") or {}).get("id") != "Open"
            or support["applications"].get("complete") is not True
            or support["applications"]["links"]
            or support["applications"]["documents"]
            or any(_money(before[k]) != 0 for k in ("taxTotal", "taxRate", "discountTotal", "shippingCost"))
            or before.get("discountItem") is not None
            or _money(before.get("discountRate") or 0) != 0
            or reference(before, "postingPeriod") != str(p["period"]["id"])
            or any(p["period"].get(k) is not False for k in ("closed", "arLocked", "allLocked"))
            or not item_usable(support["item"], profile)
            or support.get("invoice_lines_complete") is not True
            or not lines
            or any(
                _money(line["amount"]) <= 0 or (line.get("itemType") or {}).get("id") == "Discount" for line in lines
            )
        ):
            return None
        discount = _money(p["expected_after"]["credit_total"])
        total = _money(p["source"]["total"])
        if sum((_money(line["amount"]) for line in lines), Decimal(0)) != _money(before["subtotal"]):
            return None
        p.update(
            kind=KIND,
            mutation_type="update",
            record_type="invoice",
            proposed_fields={"discountItem": {"id": profile.item_id}, "discountRate": _native_number(-discount)},
            expected_after={
                "total": str(total),
                "taxTotal": "0.00",
                "amountPaid": "0.00",
                "amountRemaining": str(total),
                "discountTotal": str(-discount),
            },
            approval_basis=(
                "Apply the finalized source adjustment as a flat Sales Adjustments discount on this fully unpaid "
                "invoice. Retain its original items, date, open posting period and accounting classifications. "
                "Decrease receivables and debit the configured Sales Adjustments account by the displayed amount. "
                "Source and invoice tax are zero; this proposal does not establish a tax exemption. "
                "No credit memo or cash refund is created. Approval confirms this correction to the issued invoice; "
                "fresh payment, application, period and duplicate checks run before execution."
            ),
        )
        return p
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


def is_discount_update(record_type, normalized):
    return record_type.lower() == "invoice" and any(
        str(k).lower() in {"discountitem", "discountrate"} for k in normalized.fields
    )


def review_for_card(db, tenant_id, tool_name, record_type, normalized):
    from app.services.chat.tools import parse_external_tool_name

    p = db.info.get("accounting_correction_candidate")
    parsed = parse_external_tool_name(tool_name)
    if (
        not p
        or p.get("kind") != KIND
        or p.get("tenant_id") != str(tenant_id)
        or not parsed
        or parsed[1] != "ns_updateRecord"
        or str(parsed[0]) != p["connector_id"]
        or record_type.lower() != "invoice"
        or normalized.record_id != p["record_id"]
        or normalized.record != p["proposed_fields"]
        or normalized.lines
        or not 0 <= (datetime.now(timezone.utc) - datetime.fromisoformat(p["observed_at"])).total_seconds() <= 300
    ):
        raise ValueError("An invoice discount requires fresh unpaid-invoice evidence and an exact approval card.")
    return p


async def validate_approved(db, tenant_id, tool_name, tool_input, proposal):
    """Rebuild the complete candidate under current tenant/configuration permissions."""
    from urllib.parse import urlsplit
    from uuid import UUID

    from sqlalchemy import select

    from app.models.transaction_ops import TransactionCase
    from app.services.chat.tools import parse_external_tool_name
    from app.services.chat.write_payload import normalize_write_payload
    from app.services.mcp_connector_service import get_mcp_connector
    from app.services.transaction_ops.accounting_evidence import collect_accounting_evidence
    from app.services.transaction_ops.accounting_review import accounting_context
    from app.services.transaction_ops.commercial_credits import collect_commercial_credits
    from app.services.transaction_ops.sales_credit import build_candidate, collect_support
    from app.services.transaction_ops.tax_correction import refresh_source

    p = proposal or {}
    parsed, n = parse_external_tool_name(tool_name), normalize_write_payload(tool_input)
    if (
        p.get("kind") != KIND
        or p.get("tenant_id") != str(tenant_id)
        or not parsed
        or parsed[1] != "ns_updateRecord"
        or str(parsed[0]) != p.get("connector_id")
        or tool_input.get("recordType", "").lower() != "invoice"
        or n.record_id != p.get("record_id")
        or n.record != p.get("proposed_fields")
        or n.lines
    ):
        raise ValueError("This invoice discount needs its exact evidence-bound approval.")
    connector = await get_mcp_connector(db, parsed[0], tenant_id)
    if (
        not connector
        or connector.status != "active"
        or not connector.is_enabled
        or urlsplit(connector.server_url).hostname != f"{p['scope']['netsuite_account_id']}.suitetalk.api.netsuite.com"
    ):
        raise ValueError("The approved invoice connector/account binding changed.")
    case = await db.scalar(
        select(TransactionCase).where(TransactionCase.tenant_id == tenant_id, TransactionCase.id == UUID(p["case_id"]))
    )
    if not case or case.status != "open" or case.order_reference != p["order_reference"]:
        raise ValueError("The invoice case was resolved or changed.")
    review = await accounting_context(db, tenant_id, case.scope_json, case.latest_report_json)
    if review.get("config_id") != p["config_id"] or review.get("sales_credit_profile") != p["profile"]:
        raise ValueError("The configured accounting treatment changed.")
    source = await refresh_source(db, tenant_id, p["scope"], p["order_reference"])
    if source != p["source"]:
        raise ValueError("Source evidence changed after approval preparation.")
    evidence = await collect_accounting_evidence(db, tenant_id, review, case.latest_report_json)
    await collect_commercial_credits(db, tenant_id, review, case.latest_report_json, source, evidence)
    support = await collect_support(db, tenant_id, source, case.latest_report_json, review, evidence)
    fresh = (
        build_candidate(
            tenant_id=tenant_id,
            case_id=case.id,
            source=source,
            report=case.latest_report_json,
            review=review,
            support=support,
        )
        if support
        else None
    )
    if not fresh or any(
        fresh.get(k) != p.get(k)
        for k in ("kind", "scope", "proposed_fields", "before", "period", "expected_after", "before_gl")
    ):
        raise ValueError("Invoice, payments, applications or posting controls changed. Refresh the approval.")
    if (
        fresh["support"]["invoice_lines"] != p["support"]["invoice_lines"]
        or fresh["support"]["item"] != p["support"]["item"]
    ):
        raise ValueError("Invoice lines or discount configuration changed. Refresh the approval.")


async def verify_after(db, tenant_id, p, receipt=None):
    """Prove the exact discount, untouched items and GL delta by native reads."""
    from app.services.transaction_ops.accounting_evidence import DOCUMENT_FIELDS, _project
    from app.services.transaction_ops.netsuite_reader import LINE_FIELDS, _sublist, authenticated_reader

    if isinstance(receipt, dict) and any(
        str(receipt[k]) != p["record_id"] for k in ("id", "recordId", "internalId") if receipt.get(k)
    ):
        return {"status": "needs_review", "reason": "invoice_receipt_identity_conflict", "retry_allowed": False}
    async with authenticated_reader(
        db, tenant_id, p["connection_id"], p["scope"]["netsuite_account_id"], max_api_calls=3
    ) as reader:
        raw = await reader.request(
            "GET", f"/record/v1/invoice/{_id(p['record_id'])}", params={"expandSubResources": "true"}
        )
        errors = []
        lines = _sublist(raw, "item", "invoice", LINE_FIELDS | frozenset(FIELDS), errors)
        # collect_support persists native Decimal values as exact strings. Use
        # that same representation for readback; Decimal('405.0') != '405.0'
        # otherwise falsely rejects an unchanged, successfully posted invoice.
        lines = json.loads(json.dumps(lines, default=str))
        matched = (
            str(raw.get("id")) == p["record_id"]
            and not errors
            and all(_money(raw[k]) == _money(v) for k, v in p["expected_after"].items())
            and reference(raw, "discountItem") == reference(p["proposed_fields"], "discountItem")
            and _money(raw["discountRate"]) == _money(str(p["proposed_fields"]["discountRate"]))
            and lines == p["support"]["invoice_lines"]
            and all(
                reference(raw, k) == reference(p["before"], k)
                for k in (
                    *FIELDS,
                    "subsidiary",
                    "currency",
                    "account",
                    "postingPeriod",
                    "taxItem",
                    "createdFrom",
                    "entity",
                )
            )
            and all(
                str(raw.get(k)) == str(p["before"].get(k))
                for k in ("subtotal", "taxRate", "shippingCost", "tranDate", "exchangeRate")
            )
        )
        result = await reader.request(
            "POST",
            "/query/v1/suiteql",
            params={"limit": 100, "offset": 0},
            body={
                "q": "SELECT tal.account, tal.accountingbook, tal.debit, tal.credit FROM transactionaccountingline tal "
                "JOIN transaction t ON t.id=tal.transaction "
                f"WHERE tal.transaction={_id(p['record_id'])} AND t.subsidiary={_id(p['scope']['subsidiary_id'])}"
            },
        )
        rows, complete = _collection(result)
        applied = await reader.request(
            "POST",
            "/query/v1/suiteql",
            params={"limit": 2, "offset": 0},
            body={
                "q": "SELECT l.nextdoc FROM nexttransactionlinelink l JOIN transaction t ON t.id=l.nextdoc "
                f"WHERE l.previousdoc={_id(p['record_id'])} AND t.subsidiary={_id(p['scope']['subsidiary_id'])}"
            },
        )
        links, links_complete = _collection(applied)
        matched = matched and links_complete and not links

    def balances(values):
        totals = {}
        for row in values:
            key = (str(row["account"]), str(row["accountingbook"]))
            totals[key] = totals.get(key, Decimal(0)) + _money(row.get("debit") or 0) - _money(row.get("credit") or 0)
        return {k: v for k, v in totals.items() if v}

    expected = balances(p["before_gl"])
    discount = -_money(str(p["proposed_fields"]["discountRate"]))
    for account, delta in ((p["ar_account"], -discount), (p["sales_adjustment_account"], discount)):
        key = (account, p["accounting_book"])
        expected[key] = expected.get(key, Decimal(0)) + delta
    expected = {k: v for k, v in expected.items() if v}
    return {
        "status": "verified" if matched and complete and expected == balances(rows) else "needs_review",
        "invoice": _project(raw, DOCUMENT_FIELDS),
        "gl": rows,
        "retry_allowed": False,
        "cash_settlement": "not_verified",
        "case_settlement": "not_verified",
    }
