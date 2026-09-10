"""Read and verify applied commercial credits; never create or approve a credit."""

import json
from decimal import Decimal, localcontext

from app.schemas.transaction_ops import _decimal
from app.services.transaction_ops.netsuite_reader import (
    HEADER_FIELDS,
    LINE_FIELDS,
    NetSuiteEvidenceError,
    _collection,
    _id,
    _project,
    _sublist,
    authenticated_reader,
)

MAX_CREDIT_READS = 12
APPLICATION_FIELDS = frozenset({"doc", "apply", "amount", "line", "type", "refNum", "createdFrom"})
CREDIT_FIELDS = HEADER_FIELDS | {
    "createdFrom",
    "account",
    "memo",
    "applied",
    "unapplied",
    "amountPaid",
    "amountRemaining",
    "deposit",
}


def _money(value):
    value = _decimal(value)
    if value != value.quantize(Decimal(".01")):
        raise ValueError("unsupported_currency_precision")
    return value


def source_adjustment_basis(source):
    """A narrow zero-shipping, additional-tax order with finalized order discounts.

    This proves the header arithmetic, not a statutory tax rate or that a
    particular credit is legally required. Included tax/line promotions need
    their own treatment; do not silently apply this recipe to them.
    """
    try:
        with localcontext() as context:
            context.prec = 60
            if (
                source["state"] != "complete"
                or not source.get("completed_at")
                or source.get("requires_review") not in (False, None)
                or _money(source["included_tax_total"]) != 0
                or _money(source["ship_total"]) != 0
                or _money(source["tax_total"]) != _money(source["additional_tax_total"])
            ):
                return None
            adjustments = source["adjustments"]
            if not adjustments or len({str(a["id"]) for a in adjustments}) != len(adjustments):
                return None
            for a in adjustments:
                if (
                    a.get("finalized") is not True
                    or a.get("eligible") is False
                    or a.get("adjustable_type") != "Spree::Order"
                    or str(a.get("adjustable_id")) != str(source["id"])
                    or not a.get("label")
                    or _money(a["amount"]) >= 0
                ):
                    return None
            discount = -sum((_money(a["amount"]) for a in adjustments), Decimal(0))
            items, tax, total = (_money(source[k]) for k in ("item_total", "tax_total", "total"))
            if items <= discount or total <= 0 or tax < 0:
                return None
            if _money(source["adjustment_total"]) != tax - discount or items + tax - discount != total:
                return None
            return {
                "status": "header_explained_by_order_adjustments",
                "order_reference": source["number"],
                "source_record_id": str(source["id"]),
                "currency": source["currency"],
                "item_total": str(items),
                "tax": str(tax),
                "credit_amount": str(discount),
                "gross_before_order_adjustments": str(items + tax),
                "source_total": str(total),
                "adjustments": adjustments,
                "interpretation": "The source header includes finalized order-level adjustments. Do not call it "
                "stale or wrong merely because the sum of line totals is higher. Tax legality is not determined here.",
            }
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


def _applied_to(record, invoice_id):
    rows = record.get("applications")
    if record.get("applications_complete") is not True or not isinstance(rows, list):
        raise ValueError("applications_incomplete")
    active = [r for r in rows if r.get("apply") is True]
    if len(active) != 1 or _id((active[0].get("doc") or {}).get("id")) != invoice_id:
        raise ValueError("application_scope_ambiguous")
    return _money(active[0]["amount"])


def verify_applied_credit(basis, invoice, applications, invoice_gl):
    """Require exact credit, application, cash amount and GL proof; no fuzzy money."""
    try:
        if applications.get("complete") is not True or invoice_gl.get("complete") is not True:
            return None
        invoice_id = str(invoice["id"])
        total, tax, credit = (_money(basis[k]) for k in ("source_total", "tax", "credit_amount"))
        gross = _money(invoice["total"])
        if gross != total + credit or _money(invoice["taxTotal"]) != tax:
            return None
        currency_id, subsidiary = (str(invoice[k]["id"]) for k in ("currency", "subsidiary"))
        rows = applications["links"]
        monetary = [r for r in rows if r.get("linktype") == "Payment"]
        if len({(r["nextdoc"], r["nextline"], r["previousline"]) for r in monetary}) != len(monetary):
            return None
        if any(r.get("type") not in {"CustCred", "DepAppl"} for r in monetary):
            return None
        credits, deposits = [], []
        for r in monetary:
            doc = applications["documents"][str(r["nextdoc"])]
            if (
                str(r["previousdoc"]) != invoice_id
                or str(r.get("currency")) != currency_id
                or str(doc["currency"]["id"]) != currency_id
                or str(doc["subsidiary"]["id"]) != subsidiary
                or _decimal(doc["exchangeRate"]) != _decimal(invoice["exchangeRate"])
            ):
                return None
            amount = _applied_to(doc, invoice_id)
            if amount <= 0 or amount != _money(r["foreignamount"]) or amount != _money(doc["total"]):
                return None
            if _money(doc["unapplied"]) != 0 or _money(doc["applied"]) != amount:
                return None
            (credits if r["type"] == "CustCred" else deposits).append((doc, amount))
        # The supported recipe is one existing commercial credit and verified
        # deposit applications. Split/refunded/reversed credits stay in review.
        if len(credits) != 1 or not deposits:
            return None
        cm, amount = credits[0]
        if (
            amount != credit
            or _money(cm["taxTotal"]) != 0
            or str(cm["createdFrom"]["id"]) != invoice_id
            or basis["order_reference"] not in cm.get("memo", "")
            or cm.get("lines_complete") is not True
            or len(cm["line_items"]) != 1
            or cm["line_items"][0].get("isTaxable") is not False
            or _money(cm["line_items"][0]["amount"]) != credit
        ):
            return None
        if sum((a for _, a in deposits), Decimal(0)) != total:
            return None
        if _money(invoice["amountPaid"]) != gross or _money(invoice["amountRemaining"]) != 0:
            return None
        ar_rows = [r for r in invoice_gl["rows"] if _money(r.get("debit") or "0") == gross]
        if len(ar_rows) != 1:
            return None
        ar, book = str(ar_rows[0]["account"]), str(ar_rows[0]["accountingbook"])
        nonzero_invoice = [
            r for r in invoice_gl["rows"] if _money(r.get("debit") or "0") or _money(r.get("credit") or "0")
        ]
        if any(str(r.get("accountingbook")) != book for r in nonzero_invoice) or any(
            sum((_money(r.get(side) or "0") for r in nonzero_invoice), Decimal(0)) != gross
            for side in ("debit", "credit")
        ):
            return None
        gl = applications["credit_gl"][str(cm["id"])]
        if gl.get("complete") is not True:
            return None
        posting = [r for r in gl["rows"] if _money(r.get("debit") or "0") or _money(r.get("credit") or "0")]
        if len(posting) != 2 or any(str(r.get("accountingbook")) != book for r in posting):
            return None
        ar_credit = [
            r
            for r in posting
            if str(r.get("account")) == ar
            and _money(r.get("credit") or "0") == credit
            and _money(r.get("debit") or "0") == 0
        ]
        debit = [
            r
            for r in posting
            if str(r.get("account")) != ar
            and _money(r.get("debit") or "0") == credit
            and _money(r.get("credit") or "0") == 0
        ]
        if len(ar_credit) != 1 or len(debit) != 1:
            return None
        item = applications["credit_items"][str(cm["line_items"][0]["item"]["id"])]
        if (
            item.get("isInactive") is not False
            or str(item["account"]["id"]) != str(debit[0]["account"])
            or str((cm["line_items"][0].get("itemType") or {}).get("id")) != "Discount"
        ):
            return None
        return {
            "status": "existing_credit_verified",
            "invoice_id": invoice_id,
            "credit_memo_id": str(cm["id"]),
            "credit_memo_number": cm["tranId"],
            "credit_amount": str(credit),
            "tax_amount": "0.00",
            "net_invoice_total": str(total),
            "invoice_application_status": "verified",
            "bank_processor_clearance": "not_verified",
            "sales_adjustment_account": str(debit[0]["account"]),
            "ar_account": ar,
            "accounting_book": book,
            "item_id": str(cm["line_items"][0]["item"]["id"]),
            "next_action": "Recognize the existing applied credit; do not create another credit or edit the invoice. "
            "Order total matches after the credit, tax is unchanged. Refunds and bank/processor clearance "
            "remain separate checks; Paid In Full alone does not prove settlement.",
        }
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


async def collect_commercial_credits(db, tenant_id, review, report, source, evidence):
    basis = source_adjustment_basis(source)
    if not basis:
        return
    evidence["source_order_adjustments"] = basis
    if (
        basis["order_reference"] != report.get("order_reference")
        or basis["source_record_id"] != str((report.get("source") or {}).get("record_id"))
        or basis["currency"] != (report.get("source") or {}).get("currency")
    ):
        evidence["blockers"].append("commercial_credit_source_identity_conflict")
        return
    sections = evidence.get("sections", {})
    invoices = sections.get("posting_documents", [])
    if len(invoices) != 1 or invoices[0].get("record_type") != "invoice":
        return
    invoice = invoices[0]
    if _money(invoice["total"]) != _money(basis["gross_before_order_adjustments"]) or _money(
        invoice["taxTotal"]
    ) != _money(basis["tax"]):
        return
    scope = review["scope"]
    invoice_id, subsidiary = _id(invoice["id"]), _id(scope["subsidiary_id"])
    if not invoice_id or not subsidiary:
        return
    application = {"complete": False, "links": [], "documents": {}, "credit_gl": {}, "credit_items": {}}
    sections["invoice_applications"] = application
    async with authenticated_reader(
        db, tenant_id, review["netsuite_connection_id"], scope["netsuite_account_id"], max_api_calls=MAX_CREDIT_READS
    ) as reader:
        try:
            raw = await reader.request(
                "POST",
                "/query/v1/suiteql",
                params={"limit": 50, "offset": 0},
                body={
                    "q": "SELECT l.previousdoc, l.previousline, l.nextdoc, l.nextline, l.linktype, l.foreignamount, "
                    "t.type, t.tranid, t.currency, BUILTIN.DF(t.status) AS status_name "
                    "FROM nexttransactionlinelink l JOIN transaction t ON t.id=l.nextdoc "
                    f"WHERE l.previousdoc={invoice_id} AND t.subsidiary={subsidiary}"
                },
            )
            links, complete = _collection(raw)
            application["links"] = links
            if not complete:
                raise NetSuiteEvidenceError("application_links_incomplete")
            ids = {str(r["nextdoc"]): r["type"] for r in links if r.get("linktype") == "Payment"}
            if len(ids) > 4 or any(k not in {"CustCred", "DepAppl"} for k in ids.values()):
                raise NetSuiteEvidenceError("application_scope_requires_other_treatment")
            for ident, kind in ids.items():
                if not _id(ident):
                    raise NetSuiteEvidenceError("application_id_invalid")
                record_type = "creditMemo" if kind == "CustCred" else "depositApplication"
                doc = await reader.request(
                    "GET", f"/record/v1/{record_type}/{ident}", params={"expandSubResources": "true"}
                )
                if str(doc.get("id")) != ident or str((doc.get("subsidiary") or {}).get("id")) != subsidiary:
                    raise NetSuiteEvidenceError("application_identity_conflict")
                if str((doc.get("currency") or {}).get("id")) != str(invoice["currency"]["id"]):
                    raise NetSuiteEvidenceError("application_currency_conflict")
                projected = _project(doc, CREDIT_FIELDS)
                projected["record_type"] = record_type.lower()
                problems = []
                projected["applications"] = _sublist(doc, "apply", "apply", APPLICATION_FIELDS, problems)
                projected["applications_complete"] = not problems
                if kind == "CustCred":
                    projected["line_items"] = _sublist(doc, "item", "item", LINE_FIELDS, problems)
                    projected["lines_complete"] = not problems
                    lines = projected["line_items"] or []
                    if len(lines) == 1 and (lines[0].get("itemType") or {}).get("id") == "Discount":
                        item_id = _id((lines[0].get("item") or {}).get("id"))
                        if item_id and item_id not in application["credit_items"]:
                            item = await reader.request("GET", f"/record/v1/discountItem/{item_id}")
                            if str(item.get("id")) != item_id:
                                raise NetSuiteEvidenceError("credit_item_identity_conflict")
                            application["credit_items"][item_id] = _project(
                                item, {"id", "itemId", "isInactive", "account"}
                            )
                    gl_raw = await reader.request(
                        "POST",
                        "/query/v1/suiteql",
                        params={"limit": 30, "offset": 0},
                        body={
                            "q": "SELECT tal.account, BUILTIN.DF(tal.account) AS account_name, "
                            "tal.accountingbook, tal.debit, tal.credit "
                            "FROM transactionaccountingline tal JOIN transaction t ON t.id=tal.transaction "
                            f"WHERE tal.transaction={ident} AND t.subsidiary={subsidiary}"
                        },
                    )
                    rows, complete = _collection(gl_raw)
                    application["credit_gl"][ident] = {"complete": complete, "rows": rows}
                application["documents"][ident] = projected
            application["complete"] = True
            current = await reader.request("GET", f"/record/v1/invoice/{invoice_id}")
            if any(str(current.get(k)) != str(invoice.get(k)) for k in ("id", "lastModifiedDate")) or any(
                _money(current[k]) != _money(invoice[k]) for k in ("total", "taxTotal", "amountPaid", "amountRemaining")
            ):
                application["complete"] = False
                raise NetSuiteEvidenceError("invoice_changed_during_application_read")
            resolution = verify_applied_credit(basis, invoice, application, sections.get("gl", {}).get(invoice_id, {}))
            if resolution:
                evidence["commercial_credit_resolution"] = resolution
                evidence["assessment"]["root_cause"] = "source_order_adjustment_already_credited"
                evidence["assessment"]["executable_proposal"] = None
                evidence["assessment"]["correction_ready"] = False
            else:
                evidence["blockers"].append("commercial_credit_application_not_verified")
        except (NetSuiteEvidenceError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
            evidence["blockers"].append(f"commercial_credits:{exc}")
        finally:
            evidence["native_api_calls"] = evidence.get("native_api_calls", 0) + reader.calls


async def read_commercial_credit_for_order(db, tenant_id, config, source, target, report):
    """A bounded optional read on a proven order-level discount discrepancy."""
    basis = source_adjustment_basis(source)
    orders = target.get("orders") or []
    if not basis or len(orders) != 1:
        return None
    header = orders[0].get("header") or {}
    if _money(header.get("total")) != _money(basis["gross_before_order_adjustments"]):
        return None
    order_id, subsidiary = _id(header.get("id")), _id(config.get("subsidiary_id"))
    if not order_id or not subsidiary:
        return None
    evidence = {
        "verified_connection_scope": {
            "account_id": config["netsuite_account_id"],
            "connection_id": str(config["netsuite_connection_id"]),
        },
        "sections": {},
        "blockers": [],
        "assessment": {},
    }
    try:
        async with authenticated_reader(
            db, tenant_id, config["netsuite_connection_id"], config["netsuite_account_id"], max_api_calls=4
        ) as reader:
            raw = await reader.request(
                "POST",
                "/query/v1/suiteql",
                params={"limit": 20, "offset": 0},
                body={
                    "q": "SELECT DISTINCT t.id, t.type FROM transaction t "
                    "JOIN transactionline tl ON tl.transaction=t.id "
                    f"WHERE tl.createdfrom={order_id} AND t.subsidiary={subsidiary} "
                    "AND t.type IN ('CustInvc','CashSale')"
                },
            )
            rows, complete = _collection(raw)
            if not complete or len(rows) != 1 or rows[0].get("type") != "CustInvc":
                return None
            invoice_id = _id(rows[0].get("id"))
            if not invoice_id:
                return None
            raw = await reader.request("GET", f"/record/v1/invoice/{invoice_id}")
            if (
                str(raw.get("id")) != invoice_id
                or str((raw.get("subsidiary") or {}).get("id")) != subsidiary
                or str((raw.get("currency") or {}).get("id")) != str(header["currency"]["id"])
                or str((raw.get("createdFrom") or {}).get("id")) != order_id
            ):
                return None
            invoice = _project(raw, CREDIT_FIELDS)
            invoice["record_type"] = "invoice"
            evidence["sections"]["posting_documents"] = [invoice]
            raw = await reader.request(
                "POST",
                "/query/v1/suiteql",
                params={"limit": 30, "offset": 0},
                body={
                    "q": "SELECT tal.account, tal.accountingbook, tal.debit, tal.credit "
                    "FROM transactionaccountingline tal JOIN transaction t ON t.id=tal.transaction "
                    f"WHERE tal.transaction={invoice_id} AND t.subsidiary={subsidiary}"
                },
            )
            rows, complete = _collection(raw)
            evidence["sections"]["gl"] = {invoice_id: {"rows": rows, "complete": complete}}
            evidence["native_api_calls"] = reader.calls
        review = {
            "scope": {"netsuite_account_id": config["netsuite_account_id"], "subsidiary_id": subsidiary},
            "netsuite_connection_id": config["netsuite_connection_id"],
        }
        await collect_commercial_credits(db, tenant_id, review, report, source, evidence)
        evidence["order_record_id"] = order_id
        evidence["subsidiary_id"] = subsidiary
        return json.loads(json.dumps(evidence, default=str))
    except (NetSuiteEvidenceError, ValueError, KeyError, TypeError, ArithmeticError):
        return None


def verified_commercial_adjustment(source, target, config):
    proof = target.get("commercial_credit_evidence")
    basis = source_adjustment_basis(source)
    if not isinstance(proof, dict) or not basis:
        return None
    try:
        from app.services.transaction_ops.netsuite_reader import _account

        scope = proof["verified_connection_scope"]
        order = target["orders"][0]["header"]
        if (
            _account(scope["account_id"]) != _account(config["netsuite_account_id"])
            or str(scope["connection_id"]) != str(config["netsuite_connection_id"])
            or str(proof["subsidiary_id"]) != str(config["subsidiary_id"])
            or str(proof["order_record_id"]) != str(order["id"])
        ):
            return None
        section = proof["sections"]
        invoices = section["posting_documents"]
        if (
            len(invoices) != 1
            or str(invoices[0]["createdFrom"]["id"]) != str(order["id"])
            or str(invoices[0]["subsidiary"]["id"]) != str(config["subsidiary_id"])
            or str(order["subsidiary"]["id"]) != str(config["subsidiary_id"])
            or str(invoices[0]["currency"]["id"]) != str(order["currency"]["id"])
        ):
            return None
        result = verify_applied_credit(
            basis, invoices[0], section["invoice_applications"], section["gl"][str(invoices[0]["id"])]
        )
        if (
            result
            and _money(order["total"]) == _money(invoices[0]["total"])
            and _money(order["taxTotal"]) == _money(invoices[0]["taxTotal"])
        ):
            return {
                **result,
                "kind": "applied_commercial_credit",
                "source_adjustment_ids": [str(a["id"]) for a in basis["adjustments"]],
                "account_id": _account(scope["account_id"]),
                "subsidiary_id": str(config["subsidiary_id"]),
                "verification_evidence": proof,
            }
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None
    return None
