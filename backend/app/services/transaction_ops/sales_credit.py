"""Evidence-bound commercial credit proposals. This module never sends a write.

A standalone credit's native CreatedFrom can be empty even when applied to an
invoice. The explicit application, not a guessed CreatedFrom assignment, is the
binding. Oracle: section_N1312521.html. Connector metadata for production6738075
advertises creditmemo.item.items and apply.items; execution must still be tested.
"""

import hashlib
import json
import re
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext

from app.schemas.transaction_ops import _decimal
from app.services.transaction_ops.commercial_credits import _applied_to, _money, source_adjustment_basis
from app.services.transaction_ops.netsuite_reader import _account, _id
from app.services.transaction_ops.sales_credit_profile import SalesCreditProfile


def external_id(tenant_id, scope, source, invoice_id):
    """One stable identity per source adjustment set and invoice, across retries.

    Amount/period are deliberately absent: changing them must not manufacture a
    second posting identity after an uncertain first attempt.
    """
    identity = [
        str(tenant_id),
        _account(scope["netsuite_account_id"]),
        str(scope["subsidiary_id"]),
        str(scope.get("source_connection_id") or scope.get("source_step_id")),
        str(source["id"]),
        str(invoice_id),
        sorted(str(a["id"]) for a in source["adjustments"]),
    ]
    return "ss-sales-credit-" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def duplicate_query(invoice, profile, reference, posting_key):
    """Search applied AND standalone/unapplied credits, at transaction grain."""
    profile = SalesCreditProfile.model_validate(profile)
    invoice_id, customer, currency = (
        _id(invoice[k]["id"] if k != "id" else invoice[k]) for k in ("id", "entity", "currency")
    )
    if not all((invoice_id, customer, currency)) or not re.fullmatch(r"R[0-9]{9}(?:-[A-Z0-9]+)?", reference):
        raise ValueError("commercial_credit_identity_invalid")
    if not re.fullmatch(r"ss-sales-credit-[a-f0-9]{64}", posting_key):
        raise ValueError("commercial_credit_posting_key_invalid")
    # Any same-customer credit using the configured commercial adjustment item
    # needs examination before a new one is proposed. No amount/date filter can
    # make an older, partial or unapplied duplicate disappear.
    return (
        "SELECT DISTINCT t.id, t.tranid, t.externalid FROM transaction t "
        "LEFT JOIN transactionline tl ON tl.transaction=t.id "
        f"WHERE t.type='CustCred' AND t.subsidiary={profile.subsidiary_id} "
        f"AND t.entity={customer} AND t.currency={currency} AND ("
        f"tl.createdfrom={invoice_id} OR tl.item={profile.item_id} "
        f"OR t.memo LIKE '%{reference}%' OR t.externalid='{posting_key}')"
    )


def _native_number(value):
    """REST metadata requires a JSON number; refuse a lossy JSON round trip."""
    amount = _money(value)
    number = float(amount)
    if _decimal(str(number)) != amount:
        raise ValueError("commercial_credit_amount_not_exactly_representable")
    return number


def _gl_proves(gl, debit_account, credit_account, amount, book):
    if gl.get("complete") is not True:
        return False
    rows = [r for r in gl["rows"] if _money(r.get("debit") or 0) or _money(r.get("credit") or 0)]
    return len(rows) == 2 and sorted(
        (str(r["account"]), str(r["accountingbook"]), _money(r.get("debit") or 0), _money(r.get("credit") or 0))
        for r in rows
    ) == sorted([(debit_account, book, amount, Decimal(0)), (credit_account, book, Decimal(0), amount)])


def _reference_matches(support, profile):
    cm, item = support["reference_credit"], support["item"]
    amount = _money(cm["total"])
    lines = cm["line_items"]
    return (
        str(cm["id"]) == profile.reference_credit_id
        and str(cm["subsidiary"]["id"]) == profile.subsidiary_id
        and str(cm["currency"]["id"]) == str(support["invoice"]["currency"]["id"])
        and cm.get("lines_complete") is True
        and len(lines) == 1
        and amount > 0
        and _money(cm["taxTotal"]) == 0
        and _money(cm["shippingCost"]) == 0
        and _money(cm["discountTotal"]) == 0
        and _money(lines[0]["amount"]) == amount
        and lines[0].get("isTaxable") is False
        and str(lines[0]["item"]["id"]) == profile.item_id
        and (lines[0].get("itemType") or {}).get("id") == "Discount"
        and str(item["id"]) == profile.item_id
        and item.get("isInactive") is False
        and str(item["account"]["id"]) == profile.adjustment_account_id
        and _gl_proves(
            support["reference_gl"],
            profile.adjustment_account_id,
            profile.ar_account_id,
            amount,
            profile.accounting_book_id,
        )
    )


def build_candidate(*, tenant_id, case_id, source, report, review, support, now=None):
    """Return an exact candidate only for a fully evidenced missing reseller credit."""
    now = now or datetime.now(timezone.utc)
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            profile = SalesCreditProfile.model_validate(review["sales_credit_profile"])
            basis = source_adjustment_basis(source)
            invoice = support["invoice"]
            linked = support["linked_documents"]
            posting_sales = [r for r in linked["rows"] if r.get("type") in {"CustInvc", "CashSale"}]
            if linked.get("complete") is not True or len(posting_sales) != 1:
                return None
            if str(posting_sales[0].get("id")) != str(invoice["id"]) or posting_sales[0]["type"] != "CustInvc":
                return None
            scope = review["scope"]
            if (
                not basis
                or review.get("configuration_status") != "scoped_configuration_found"
                or not review.get("connection_active")
                or not review.get("native_mcp_connector_id")
                or _account(scope["netsuite_account_id"]) != profile.account_id
                or str(scope["subsidiary_id"]) != profile.subsidiary_id
                or source["currency"] != profile.currency
                or report["source"]["currency_minor_unit"] != 2
                or basis["order_reference"] != report["order_reference"]
                or basis["source_record_id"] != str(report["source"]["record_id"])
                or report["source"]["currency"] != profile.currency
                or review["business_entity_subsidiaries"].get(source["business_entity"]) != profile.subsidiary_id
                or any(a["label"] != profile.source_adjustment_label for a in source["adjustments"])
                or str(invoice["subsidiary"]["id"]) != profile.subsidiary_id
                or str(support["currency"]["id"]) != str(invoice["currency"]["id"])
                or support["currency"].get("symbol") != profile.currency
                or invoice.get("record_type") != "invoice"
                or not invoice.get("lastModifiedDate")
                or str(invoice["createdFrom"]["id"]) != str(support["order_id"])
                or _decimal(invoice["exchangeRate"]) != 1
                or str(invoice["account"]["id"]) != profile.ar_account_id
                or not _reference_matches(support, profile)
            ):
                return None
            if not all(_id(invoice[k]["id"]) for k in ("entity", "currency", "account", "subsidiary")):
                return None
            observed = datetime.fromisoformat(support["observed_at"].replace("Z", "+00:00"))
            if not 0 <= (now - observed).total_seconds() <= 300:
                return None
            gross, total, credit, tax = (
                _money(basis[k]) for k in ("gross_before_order_adjustments", "source_total", "credit_amount", "tax")
            )
            paid = _money(source["payment_total"])
            remaining = total - paid
            if (
                _money(invoice["total"]) != gross
                or _money(invoice["subtotal"]) != _money(source["item_total"])
                or _money(invoice["taxTotal"]) != tax
                or _money(invoice["amountRemaining"]) != gross - paid
                or _money(invoice["amountPaid"]) != paid
                or _money(invoice["shippingCost"]) != 0
                or _money(invoice["discountTotal"]) != 0
                or not 0 <= paid <= total
                or source["payment_state"] != ("paid" if remaining == 0 else "balance_due")
                or support["duplicates"].get("complete") is not True
                or support["duplicates"]["rows"]
            ):
                return None
            if any(_money(report["balance"]["amounts"]["refunds"][k]) != 0 for k in ("source", "target", "delta")):
                return None
            for side in ("source", "target"):
                refund = support["refunds"][side]
                if (
                    refund.get("complete") is not True
                    or _money(refund["amount"]) != 0
                    or refund.get("order_reference") != source["number"]
                    or refund.get("currency") != profile.currency
                    or not 0
                    <= (now - datetime.fromisoformat(refund["observed_at"].replace("Z", "+00:00"))).total_seconds()
                    <= 300
                ):
                    return None
            applications = support["applications"]
            if applications.get("complete") is not True:
                return None
            links = applications["links"]
            payments = [r for r in links if r.get("linktype") == "Payment"]
            if len(payments) != len(links) or any(r.get("type") != "DepAppl" for r in links):
                return None
            if len({str(r["nextdoc"]) for r in payments}) != len(payments):
                return None
            applied = Decimal(0)
            for link in payments:
                doc = applications["documents"][str(link["nextdoc"])]
                amount = _applied_to(doc, str(invoice["id"]))
                if (
                    str(link["previousdoc"]) != str(invoice["id"])
                    or str(link["currency"]) != str(invoice["currency"]["id"])
                    or str(doc["subsidiary"]["id"]) != profile.subsidiary_id
                    or str(doc["currency"]["id"]) != str(invoice["currency"]["id"])
                    or _decimal(doc["exchangeRate"]) != 1
                    or amount <= 0
                    or amount != _money(link["foreignamount"])
                    or amount != _money(doc["applied"])
                    or amount != _money(doc["total"])
                    or _money(doc["unapplied"]) != 0
                ):
                    return None
                applied += amount
            if applied != paid:
                return None
            gl = support["invoice_gl"]
            if gl.get("complete") is not True or not gl["rows"]:
                return None
            posting = [r for r in gl["rows"] if _money(r.get("debit") or 0) or _money(r.get("credit") or 0)]
            if any(str(r["accountingbook"]) != profile.accounting_book_id for r in posting):
                return None
            if any(sum((_money(r.get(k) or 0) for r in posting), Decimal(0)) != gross for k in ("debit", "credit")):
                return None
            ar = [r for r in posting if str(r["account"]) == profile.ar_account_id]
            if len(ar) != 1 or _money(ar[0].get("debit")) != gross or _money(ar[0].get("credit") or 0) != 0:
                return None
            period = support["period"]
            posting_date = date.fromisoformat(support["posting_date"])
            if (
                any(
                    period.get(k) is not False
                    for k in ("closed", "arLocked", "allLocked", "isAdjust", "isYear", "isQuarter")
                )
                or not _id(period["id"])
                or not date.fromisoformat(period["startDate"]) <= posting_date <= date.fromisoformat(period["endDate"])
            ):
                return None
            key = external_id(tenant_id, scope, source, invoice["id"])
            if support["duplicates"].get("posting_key") != key:
                return None
            fields = {
                "entity": {"id": str(invoice["entity"]["id"])},
                "subsidiary": {"id": profile.subsidiary_id},
                "currency": {"id": str(invoice["currency"]["id"])},
                "account": {"id": profile.ar_account_id},
                "externalId": key,
                "tranDate": posting_date.isoformat(),
                "postingPeriod": {"id": str(period["id"])},
                "memo": f"{source['number']} {profile.source_adjustment_label}",
                "autoApply": False,
                "toBeEmailed": False,
                "item": {
                    "items": [
                        {
                            "item": {"id": profile.item_id},
                            "rate": _native_number(credit),
                            "amount": _native_number(credit),
                            "isTaxable": False,
                        }
                    ]
                },
                "apply": {
                    "items": [{"doc": {"id": str(invoice["id"])}, "apply": True, "amount": _native_number(credit)}]
                },
            }
            return deepcopy(
                {
                    "kind": "sales_adjustment_credit",
                    "mutation_type": "create",
                    "record_type": "creditmemo",
                    "record_id": str(invoice["id"]),
                    "lock_record_type": "invoice",
                    "tenant_id": str(tenant_id),
                    "case_id": str(case_id),
                    "scope": scope,
                    "connector_id": review["native_mcp_connector_id"],
                    "connection_id": review["netsuite_connection_id"],
                    "config_id": review["config_id"],
                    "profile": profile.model_dump(mode="json"),
                    "order_reference": source["number"],
                    "source": source,
                    "before": invoice,
                    "before_gl": gl["rows"],
                    "period": period,
                    "proposed_fields": fields,
                    "expected_after": {
                        "credit_total": str(credit),
                        "credit_tax": "0.00",
                        "net_invoice_total": str(total),
                        "invoice_total": str(gross),
                        "invoice_tax": str(tax),
                        "invoice_remaining": f"{remaining:.2f}",
                        "remaining_variance": "0.00",
                    },
                    "observed_at": support["observed_at"],
                    "support": support,
                    "ar_account": profile.ar_account_id,
                    "sales_adjustment_account": profile.adjustment_account_id,
                    "accounting_book": profile.accounting_book_id,
                    "approval_basis": "Approve this finalized source commercial adjustment as a non-taxable Sales "
                    "Adjustments credit and apply only to the displayed invoice. Finance approval confirms this "
                    "treatment and posting date. This credits receivables, leaves invoice tax unchanged, and does "
                    "not issue a cash refund or prove bank/processor settlement.",
                }
            )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


async def collect_support(db, tenant_id, source, report, review, evidence, *, now=None):
    """Bounded reads for the supported missing-credit case, before any proposal."""
    from zoneinfo import ZoneInfo

    from app.services.transaction_ops import state_service
    from app.services.transaction_ops.commercial_credits import CREDIT_FIELDS
    from app.services.transaction_ops.netsuite_reader import (
        LINE_FIELDS,
        _collection,
        _project,
        _sublist,
        authenticated_reader,
        read_netsuite_order,
    )
    from app.services.transaction_ops.netsuite_refunds import read_netsuite_refunds
    from app.services.transaction_ops.periods import ReconciliationPolicy
    from app.services.transaction_ops.refund_reader import read_solidus_refunds

    now = now or datetime.now(timezone.utc)
    profile = SalesCreditProfile.model_validate(review["sales_credit_profile"])
    basis = source_adjustment_basis(source)
    docs = evidence.get("sections", {}).get("posting_documents", [])
    if not basis or evidence.get("commercial_credit_resolution") or len(docs) != 1:
        return None
    invoice = docs[0]
    if invoice.get("record_type") != "invoice" or _money(invoice.get("amountRemaining")) < _money(
        basis["credit_amount"]
    ):
        return None
    if review.get("configuration_status") != "scoped_configuration_found" or not review.get("connection_active"):
        return None
    scope = review["scope"]
    if profile.account_id != _account(scope["netsuite_account_id"]) or profile.subsidiary_id != str(
        scope["subsidiary_id"]
    ):
        return None
    config = await state_service.get_config(db, tenant_id, review["config_id"])
    mapping = config.mapping_json
    if not config.enabled or mapping.get("sales_credit_profile") != profile.model_dump(mode="json"):
        return None
    if not mapping.get("solidus_refund_step_id"):
        raise ValueError("commercial_credit_fresh_refund_source_unavailable")
    posting_date = now.astimezone(
        ZoneInfo(ReconciliationPolicy.model_validate(mapping.get("reconciliation_policy") or {}).timezone_name)
    ).date()
    order_id = _id((invoice.get("createdFrom") or {}).get("id"))
    invoice_id = _id(invoice.get("id"))
    if not order_id or not invoice_id:
        return None
    support = {
        "invoice": invoice,
        "linked_documents": evidence["sections"]["linked_documents"],
        "order_id": order_id,
        "posting_date": posting_date.isoformat(),
        "applications": evidence["sections"]["invoice_applications"],
        "invoice_gl": evidence["sections"]["gl"][invoice_id],
    }
    key = external_id(tenant_id, scope, source, invoice_id)
    async with authenticated_reader(
        db, tenant_id, review["netsuite_connection_id"], profile.account_id, max_api_calls=10
    ) as reader:
        duplicate_raw = await reader.request(
            "POST",
            "/query/v1/suiteql",
            params={"limit": 100, "offset": 0},
            body={"q": duplicate_query(invoice, profile, source["number"], key)},
        )
        rows, complete = _collection(duplicate_raw)
        support["duplicates"] = {"rows": rows, "complete": complete, "posting_key": key}
        if not complete or rows:
            evidence["sales_credit_blocker"] = (
                "Existing or potentially related credits require review; do not create another."
            )
            evidence["sales_credit_existing_records"] = rows
            return None
        support["currency"] = await reader.request("GET", f"/record/v1/currency/{_id(invoice['currency']['id'])}")
        support["item"] = _project(
            await reader.request("GET", f"/record/v1/discountItem/{profile.item_id}"),
            {"id", "itemId", "isInactive", "account"},
        )
        raw = await reader.request(
            "GET", f"/record/v1/creditMemo/{profile.reference_credit_id}", params={"expandSubResources": "true"}
        )
        cm = _project(raw, CREDIT_FIELDS)
        errors = []
        cm["line_items"] = _sublist(raw, "item", "item", LINE_FIELDS, errors)
        cm["lines_complete"] = not errors
        support["reference_credit"] = cm
        raw = await reader.request(
            "POST",
            "/query/v1/suiteql",
            params={"limit": 30, "offset": 0},
            body={
                "q": "SELECT tal.account, tal.accountingbook, tal.debit, tal.credit FROM transactionaccountingline tal "
                "JOIN transaction t ON t.id=tal.transaction "
                f"WHERE tal.transaction={profile.reference_credit_id} AND t.subsidiary={profile.subsidiary_id}"
            },
        )
        rows, complete = _collection(raw)
        support["reference_gl"] = {"rows": rows, "complete": complete}
        raw = await reader.request(
            "POST",
            "/query/v1/suiteql",
            params={"limit": 10, "offset": 0},
            body={
                "q": "SELECT id FROM accountingperiod WHERE isyear='F' AND isquarter='F' AND isadjust='F' "
                f"AND startdate<=TO_DATE('{posting_date.isoformat()}','YYYY-MM-DD') "
                f"AND enddate>=TO_DATE('{posting_date.isoformat()}','YYYY-MM-DD')"
            },
        )
        rows, complete = _collection(raw)
        if not complete or len(rows) != 1 or not _id(rows[0].get("id")):
            raise ValueError("commercial_credit_current_period_ambiguous")
        support["period"] = await reader.request("GET", f"/record/v1/accountingPeriod/{_id(rows[0]['id'])}")
        # The full refund graph includes cash returned through deposits as well
        # as invoice/credit paths. A zero in an older report is not fresh proof.
        target = await read_netsuite_order(
            db,
            tenant_id,
            review["netsuite_connection_id"],
            profile.account_id,
            profile.subsidiary_id,
            source["number"],
            mapping["reference_field"],
        )
        if len(target.get("orders", [])) != 1 or str(target["orders"][0]["record_id"]) != order_id:
            raise ValueError("commercial_credit_order_identity_changed")
        support["refunds"] = {
            "source": await read_solidus_refunds(db, tenant_id, mapping["solidus_refund_step_id"], source["number"]),
            "target": await read_netsuite_refunds(
                db,
                tenant_id,
                review["netsuite_connection_id"],
                profile.account_id,
                profile.subsidiary_id,
                source["number"],
                target,
            ),
        }
        latest = await reader.request("GET", f"/record/v1/invoice/{invoice_id}")
        if any(str(latest.get(k)) != str(invoice.get(k)) for k in ("id", "lastModifiedDate")):
            raise ValueError("commercial_credit_invoice_changed_during_read")
    support["observed_at"] = datetime.now(timezone.utc).isoformat()
    return json.loads(json.dumps(support, default=str))


def review_for_card(db, tenant_id, tool_name, record_type, normalized):
    from app.services.chat.tools import parse_external_tool_name

    cached = getattr(db, "info", {}).get("accounting_correction_candidate")
    parsed = parse_external_tool_name(tool_name)
    if (
        not cached
        or cached.get("kind") != "sales_adjustment_credit"
        or cached.get("tenant_id") != str(tenant_id)
        or not parsed
        or parsed[1] != "ns_createRecord"
        or str(parsed[0]) != cached["connector_id"]
        or record_type.lower() != "creditmemo"
        or normalized.record_id is not None
        or normalized.record != cached["proposed_fields"]
        or not 0 <= (datetime.now(timezone.utc) - datetime.fromisoformat(cached["observed_at"])).total_seconds() <= 300
    ):
        raise ValueError(
            "This credit creation requires fresh case evidence and an exact Sales Adjustments approval card."
        )
    return cached


async def validate_approved(db, tenant_id, tool_name, tool_input, proposal):
    from urllib.parse import urlsplit

    from app.services.chat.tools import parse_external_tool_name
    from app.services.chat.write_payload import normalize_write_payload
    from app.services.mcp_connector_service import get_mcp_connector
    from app.services.transaction_ops.accounting_evidence import collect_accounting_evidence
    from app.services.transaction_ops.accounting_review import accounting_context
    from app.services.transaction_ops.commercial_credits import collect_commercial_credits
    from app.services.transaction_ops.tax_correction import refresh_source

    parsed = parse_external_tool_name(tool_name)
    normalized = normalize_write_payload(tool_input)
    if (
        not proposal
        or proposal.get("kind") != "sales_adjustment_credit"
        or proposal.get("tenant_id") != str(tenant_id)
        or not parsed
        or parsed[1] != "ns_createRecord"
        or str(parsed[0]) != proposal["connector_id"]
        or tool_input.get("recordType", "").lower() != "creditmemo"
        or normalized.record_id is not None
        or normalized.record != proposal["proposed_fields"]
    ):
        raise ValueError("This credit creation needs its exact evidence-bound approval.")
    connector = await get_mcp_connector(db, parsed[0], tenant_id)
    if (
        not connector
        or urlsplit(connector.server_url).hostname
        != f"{proposal['scope']['netsuite_account_id']}.suitetalk.api.netsuite.com"
    ):
        raise ValueError("The approved credit connector/account binding changed.")
    # Resolve the case again under current tenant/configuration controls. The
    # stored proposal does not give a removed or rebound configuration authority.
    from sqlalchemy import select

    from app.models.transaction_ops import TransactionCase

    case = await db.scalar(
        select(TransactionCase).where(TransactionCase.tenant_id == tenant_id, TransactionCase.id == proposal["case_id"])
    )
    if not case or case.status != "open" or case.order_reference != proposal["order_reference"]:
        raise ValueError("The credit case was resolved or changed. Do not create another credit.")
    review = await accounting_context(db, tenant_id, case.scope_json, case.latest_report_json)
    if review.get("config_id") != proposal["config_id"] or review.get("sales_credit_profile") != proposal["profile"]:
        raise ValueError("The configured credit treatment changed. A fresh approval is required.")
    source = await refresh_source(db, tenant_id, proposal["scope"], proposal["order_reference"])
    if source != proposal["source"]:
        raise ValueError("Source evidence changed after the credit proposal.")
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
    if (
        not fresh
        or any(fresh[k] != proposal[k] for k in ("proposed_fields", "before", "period"))
        or sorted(fresh["before_gl"], key=lambda row: json.dumps(row, sort_keys=True))
        != sorted(proposal["before_gl"], key=lambda row: json.dumps(row, sort_keys=True))
    ):
        raise ValueError(
            "Invoice, credits, applications, refund evidence or posting controls changed. Refresh the approval."
        )


async def verify_after(db, tenant_id, proposal, receipt):
    """Look up the stable external ID and prove the exact credit and application."""
    from app.services.transaction_ops.commercial_credits import collect_commercial_credits
    from app.services.transaction_ops.netsuite_reader import _collection, authenticated_reader
    from app.services.transaction_ops.tax_correction import refresh_source

    profile = SalesCreditProfile.model_validate(proposal["profile"])
    invoice_id = _id(proposal["record_id"])
    key = proposal["proposed_fields"]["externalId"]
    if key != external_id(tenant_id, proposal["scope"], proposal["source"], invoice_id):
        raise ValueError("commercial_credit_posting_identity_changed")
    evidence = {"sections": {}, "assessment": {}, "blockers": []}
    async with authenticated_reader(
        db, tenant_id, proposal["connection_id"], profile.account_id, max_api_calls=4
    ) as reader:
        raw = await reader.request(
            "POST",
            "/query/v1/suiteql",
            params={"limit": 2, "offset": 0},
            body={
                "q": f"SELECT id FROM transaction WHERE type='CustCred' AND subsidiary={profile.subsidiary_id} "
                f"AND externalid='{key}'"
            },
        )
        rows, complete = _collection(raw)
        if not complete or len(rows) != 1 or not _id(rows[0].get("id")):
            return {"status": "needs_review", "reason": "credit_receipt_not_uniquely_verified", "retry_allowed": False}
        credit_id = _id(rows[0]["id"])
        receipt_id = (
            next((receipt[k] for k in ("recordId", "id", "internalId") if receipt.get(k)), None)
            if isinstance(receipt, dict)
            else None
        )
        if receipt_id is not None and _id(receipt_id) != credit_id:
            return {"status": "needs_review", "reason": "credit_receipt_identity_conflict", "retry_allowed": False}
        doc = await reader.request("GET", f"/record/v1/invoice/{invoice_id}")
        for field in ("entity", "account", "currency", "subsidiary", "createdFrom", "postingPeriod"):
            if str((doc.get(field) or {}).get("id")) != str((proposal["before"].get(field) or {}).get("id")):
                return {
                    "status": "needs_review",
                    "reason": "invoice_identity_or_account_changed",
                    "retry_allowed": False,
                }
        doc["record_type"] = "invoice"
        evidence["sections"]["posting_documents"] = [doc]
        raw = await reader.request(
            "POST",
            "/query/v1/suiteql",
            params={"limit": 30, "offset": 0},
            body={
                "q": "SELECT tal.account, tal.accountingbook, tal.debit, tal.credit FROM transactionaccountingline tal "
                "JOIN transaction t ON t.id=tal.transaction "
                f"WHERE tal.transaction={invoice_id} AND t.subsidiary={profile.subsidiary_id}"
            },
        )
        rows, complete = _collection(raw)
        evidence["sections"]["gl"] = {invoice_id: {"rows": rows, "complete": complete}}
    source = await refresh_source(db, tenant_id, proposal["scope"], proposal["order_reference"])
    report = {
        "order_reference": proposal["order_reference"],
        "source": {"record_id": str(source["id"]), "currency": source["currency"]},
    }
    review = {"scope": proposal["scope"], "netsuite_connection_id": proposal["connection_id"]}
    await collect_commercial_credits(db, tenant_id, review, report, source, evidence)
    resolution = evidence.get("commercial_credit_resolution") or {}
    cm = evidence["sections"].get("invoice_applications", {}).get("documents", {}).get(credit_id, {})

    def invoice_gl_signature(rows):
        return sorted(
            (str(r["account"]), str(r["accountingbook"]), _money(r.get("debit") or 0), _money(r.get("credit") or 0))
            for r in rows
            if _money(r.get("debit") or 0) or _money(r.get("credit") or 0)
        )

    matches = (
        source == proposal["source"]
        and resolution.get("credit_memo_id") == credit_id
        and resolution.get("sales_adjustment_account") == profile.adjustment_account_id
        and resolution.get("ar_account") == profile.ar_account_id
        and resolution.get("accounting_book") == profile.accounting_book_id
        and resolution.get("item_id") == profile.item_id
        and _money(resolution.get("invoice_remaining")) == _money(proposal["expected_after"]["invoice_remaining"])
        and invoice_gl_signature(evidence["sections"]["gl"][invoice_id]["rows"])
        == invoice_gl_signature(proposal["before_gl"])
        and str((cm.get("postingPeriod") or {}).get("id")) == str(proposal["period"]["id"])
        and cm.get("tranDate") == proposal["proposed_fields"]["tranDate"]
        and str((cm.get("entity") or {}).get("id")) == str(proposal["before"]["entity"]["id"])
    )
    return {
        "status": "verified" if matches else "needs_review",
        "credit_memo_id": credit_id,
        "resolution": resolution,
        "evidence": json.loads(json.dumps(evidence, default=str)),
        "cash_settlement": "not_verified",
        "case_settlement": "not_verified",
        "retry_allowed": False,
    }
