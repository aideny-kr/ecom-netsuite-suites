"""Narrow source-backed invoice tax-rate proposals; never approve or write here."""

import json
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from app.schemas.transaction_ops import _decimal
from app.services.transaction_ops.source_reader import read_framework_order

SOURCE_FIELDS = (
    "business_entity",
    "id",
    "number",
    "state",
    "currency",
    "updated_at",
    "completed_at",
    "requires_review",
    "total",
    "tax_total",
    "included_tax_total",
    "additional_tax_total",
    "ship_total",
    "adjustment_total",
    "item_total",
    "line_items",
    "adjustments",
    "payment_total",
    "payment_state",
    "payments",
)


async def refresh_source(db, tenant_id, scope, reference):
    import uuid

    envelope = await read_framework_order(
        db,
        tenant_id,
        uuid.UUID(scope["source_step_id"]) if scope.get("source_step_id") else None,
        reference,
        source_connection_id=uuid.UUID(scope["source_connection_id"]) if scope.get("source_connection_id") else None,
    )
    return json.loads(
        json.dumps({k: envelope["orders"][0][k] for k in SOURCE_FIELDS if k in envelope["orders"][0]}, default=str)
    )


def candidate(evidence, report, review, source):
    """Correct only a proven inclusive-tax denominator mismatch.

    Preserve finalized source tax with a seven-decimal integration rate. Never
    choose a nominal/statutory rate by searching for a number that balances.
    """
    try:
        if (
            not review.get("native_mcp_connector_id")
            or source.get("state") != "complete"
            or source.get("requires_review") not in (False, None)
            or not source.get("completed_at")
            or source.get("number") != report["order_reference"]
            or str(source.get("id")) != str(report["source"]["record_id"])
            or source.get("currency") != report["source"]["currency"]
            or review.get("business_entity_subsidiaries", {}).get(source.get("business_entity"))
            != review["scope"]["subsidiary_id"]
            or report["source"].get("currency_minor_unit") != 2
        ):
            return None
        sections = evidence["sections"]
        docs = sections["posting_documents"]
        if len(docs) != 1 or not sections["linked_documents"]["complete"]:
            return None
        doc = docs[0]
        if not doc.get("lastModifiedDate") or doc.get("record_type") != "invoice":
            return None
        total, tax = _decimal(source["total"]), _decimal(source["tax_total"])
        if total != _decimal(report["source"]["total"]) or tax != _decimal(report["source"]["tax"]):
            return None
        net = total - tax
        before_total, before_tax = _decimal(doc["total"]), _decimal(doc["taxTotal"])
        if (
            tax <= 0
            or net <= 0
            or before_tax <= 0
            or total <= before_total
            or _decimal(source["included_tax_total"]) != tax
            or any(_decimal(source[k]) != 0 for k in ("additional_tax_total", "ship_total", "adjustment_total"))
            or _decimal(doc["subtotal"]) != net
            or before_total != net + before_tax
            or _decimal(doc["exchangeRate"]) != 1
            or (net * _decimal(doc["taxRate"]) / 100).quantize(Decimal(".01"), rounding=ROUND_HALF_UP) != before_tax
        ):
            return None
        tax_items = [item for item in sections.get("taxItem", []) if str(item.get("id")) == str(doc["taxItem"]["id"])]
        if len(tax_items) != 1 or tax_items[0].get("isInactive") is not False:
            return None
        period_id = doc["postingPeriod"]["id"]
        periods = [p for p in sections.get("postingPeriod", []) if str(p["id"]) == str(period_id)]
        if len(periods) != 1 or periods[0].get("closed") is not False:
            return None
        period = periods[0]
        if any(type(period.get(k)) is not bool for k in ("arLocked", "allLocked")):
            return None
        gl = sections.get("gl", {}).get(str(doc["id"]))
        if not gl or not gl["complete"] or not gl["rows"]:
            return None
        # One book / one debit AR and tax credit: do not invent multi-book allocation.
        if len({str(r.get("accountingbook")) for r in gl["rows"]}) != 1:
            return None
        ar = [r for r in gl["rows"] if r.get("debit") is not None and _decimal(r["debit"]) == before_total]
        tax_rows = [r for r in gl["rows"] if r.get("credit") is not None and _decimal(r["credit"]) == before_tax]
        if len(ar) != 1 or len(tax_rows) != 1 or ar[0]["account"] == tax_rows[0]["account"]:
            return None
        # This specific repair needs finalized per-line VAT, not just a desired total.
        lines = source["line_items"]
        if not lines or len({str(line["id"]) for line in lines}) != len(lines):
            return None
        gross_sum = Decimal(0)
        tax_sum = Decimal(0)
        for line in lines:
            gross = _decimal(line["total"])
            adjustments = line["adjustments"]
            if gross <= 0 or not adjustments:
                return None
            if any(
                a.get("source_type") != "Spree::TaxRate"
                or a.get("finalized") is not True
                or a.get("label") != "VAT (Included in Price)"
                or _decimal(a["amount"]) < 0
                for a in adjustments
            ):
                return None
            line_tax = sum((_decimal(a["amount"]) for a in adjustments), Decimal(0))
            if not 0 < line_tax < gross:
                return None
            gross_sum += gross
            tax_sum += line_tax
        if gross_sum != total or tax_sum != tax or _decimal(source["item_total"]) != total:
            return None
        precision = Decimal(".0000001")
        wrong_rate = (tax / total * 100).quantize(precision, rounding=ROUND_HALF_UP)
        if _decimal(doc["taxRate"]) != wrong_rate:
            return None
        rate = (tax / net * 100).quantize(precision, rounding=ROUND_HALF_UP)
        if (net * rate / 100).quantize(Decimal(".01"), rounding=ROUND_HALF_UP) != tax:
            return None
        return {
            "record_type": "invoice",
            "record_id": str(doc["id"]),
            "connector_id": review["native_mcp_connector_id"],
            "connection_id": review["netsuite_connection_id"],
            "scope": review["scope"],
            "order_reference": report["order_reference"],
            "source": source,
            "before": doc,
            "before_gl": gl["rows"],
            "proposed_fields": {"taxRate": float(rate)},
            "expected_after": {"total": str(total), "taxTotal": str(tax)},
            "ar_account": str(ar[0]["account"]),
            "ar_account_name": ar[0].get("account_name"),
            "tax_account_name": tax_rows[0].get("account_name"),
            "accounting_book": str(ar[0]["accountingbook"]),
            "tax_account": str(tax_rows[0]["account"]),
            "period": period,
            "tax_item": tax_items[0],
            "observed_at": evidence["observed_at"],
            "approval_basis": "Finance approval confirms the source tax basis and retaining "
            "the displayed existing tax account/agency. "
            "Their jurisdictional classification has not been independently validated. "
            "Approve restoring the invoice tax to the finalized source tax using the displayed "
            "effective integration rate (source VAT / net subtotal, seven decimals). "
            "This does not determine a new statutory rate. "
            "The current rate uses the gross denominator despite finalized included VAT. "
            "This changes posted AR and tax; it does not prove the historical integration execution. "
            "The existing posting period is retained. If locked, execution requires the connected role's existing "
            "Override Period Restrictions permission; no period is reopened or permission changed. "
            "The sales order and any unapplied deposit need separate verification and approval; "
            "this card does not settle cash.",
        }
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


def is_tax_update(record_type, fields):
    return record_type.lower() in {"invoice", "salesorder", "cashsale", "creditmemo"} and any(
        "tax" in str(k).lower() for k in fields
    )


def review_for_card(db, tenant_id, tool_name, record_type, normalized):
    if not is_tax_update(record_type, normalized.fields):
        return None
    from app.services.chat.tools import parse_external_tool_name

    parsed = parse_external_tool_name(tool_name)
    cached = getattr(db, "info", {}).get("accounting_correction_candidate")
    if not cached or not parsed or cached.get("tenant_id") != str(tenant_id):
        raise ValueError("Read transaction_ops_accounting_evidence for this case before proposing a tax correction.")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(cached["observed_at"])).total_seconds()
    if (
        not 0 <= age <= 300
        or str(parsed[0]) != cached["connector_id"]
        or record_type.lower() != cached["record_type"]
        or normalized.record_id != cached["record_id"]
        or normalized.fields != cached["proposed_fields"]
        or normalized.lines
    ):
        raise ValueError(
            "Tax correction does not match fresh scoped evidence. Refresh the case; do not guess another payload."
        )
    return cached


async def revalidate(db, tenant_id, proposal):
    """Cheap fresh read of the exact records before the approved external call."""
    from app.services.transaction_ops.netsuite_reader import authenticated_reader

    source = await refresh_source(db, tenant_id, proposal["scope"], proposal["order_reference"])
    if source != proposal["source"]:
        raise ValueError("Source evidence changed after the proposal. A fresh approval card is required.")
    async with authenticated_reader(
        db, tenant_id, proposal["connection_id"], proposal["scope"]["netsuite_account_id"]
    ) as reader:
        doc = await reader.request("GET", f"/record/v1/invoice/{proposal['record_id']}")
        for key in (
            "id",
            "lastModifiedDate",
            "total",
            "taxTotal",
            "taxRate",
            "subtotal",
            "amountPaid",
            "amountRemaining",
        ):
            unchanged = (
                str(doc.get(key)) == str(proposal["before"].get(key))
                if key in {"id", "lastModifiedDate"}
                else _decimal(doc[key]) == _decimal(proposal["before"][key])
            )
            if not unchanged:
                raise ValueError(f"NetSuite {key} changed after the proposal. A fresh approval card is required.")
        for key in ("subsidiary", "currency", "postingPeriod", "taxItem", "createdFrom"):
            if str((doc.get(key) or {}).get("id")) != str((proposal["before"].get(key) or {}).get("id")):
                raise ValueError(f"NetSuite {key} changed after the proposal.")
        period = await reader.request("GET", f"/record/v1/accountingPeriod/{proposal['period']['id']}")
        if any(period.get(k) != proposal["period"].get(k) for k in ("closed", "arLocked", "allLocked")):
            raise ValueError("Posting-period controls changed after the proposal. A fresh approval card is required.")
        item = await reader.request("GET", f"/record/v1/salesTaxItem/{proposal['tax_item']['id']}")
        saved = proposal["tax_item"]
        if (
            item.get("isInactive") is not False
            or str(item.get("id")) != str(saved["id"])
            or item.get("itemId") != saved.get("itemId")
            or str(item.get("rate")) != str(saved.get("rate"))
            or (item.get("taxAgency") or {}).get("id") != (saved.get("taxAgency") or {}).get("id")
        ):
            raise ValueError("Tax-item configuration changed after the proposal. A fresh approval card is required.")


async def verify_after(db, tenant_id, proposal):
    from app.services.transaction_ops.netsuite_reader import _collection, authenticated_reader

    async with authenticated_reader(
        db, tenant_id, proposal["connection_id"], proposal["scope"]["netsuite_account_id"]
    ) as reader:
        doc = await reader.request("GET", f"/record/v1/invoice/{proposal['record_id']}")
        fields = {
            k: doc.get(k)
            for k in ("id", "total", "taxTotal", "taxRate", "amountPaid", "amountRemaining", "lastModifiedDate")
        }
        matched = all(_decimal(doc[k]) == _decimal(v) for k, v in proposal["expected_after"].items())
        matched = matched and _decimal(doc["taxRate"]) == _decimal(str(proposal["proposed_fields"]["taxRate"]))
        matched = matched and str(doc.get("id")) == proposal["record_id"]
        matched = matched and all(
            str((doc.get(k) or {}).get("id")) == str((proposal["before"].get(k) or {}).get("id"))
            for k in ("subsidiary", "currency", "postingPeriod", "taxItem", "createdFrom")
        )
        raw = await reader.request(
            "POST",
            "/query/v1/suiteql",
            params={"limit": 30, "offset": 0},
            body={
                "q": "SELECT tal.account, tal.accountingbook, tal.debit, tal.credit FROM transactionaccountingline tal "
                "JOIN transaction t ON t.id = tal.transaction "
                f"WHERE tal.transaction = {proposal['record_id']} "
                f"AND t.subsidiary = {proposal['scope']['subsidiary_id']}"
            },
        )
        rows, complete = _collection(raw)

        def balances(values):
            grouped = {}
            for r in values:
                key = (str(r["account"]), str(r["accountingbook"]))
                grouped[key] = (
                    grouped.get(key, Decimal(0)) + _decimal(r.get("debit") or "0") - _decimal(r.get("credit") or "0")
                )
            return grouped

        before, after = balances(proposal["before_gl"]), balances(rows)
        delta = _decimal(proposal["expected_after"]["total"]) - _decimal(proposal["before"]["total"])
        for key in before:
            before[key] += (
                delta if key[0] == proposal["ar_account"] else -delta if key[0] == proposal["tax_account"] else 0
            )
        return {
            "status": "verified" if matched and complete and before == after else "needs_review",
            "invoice": fields,
            "gl": rows,
            "cash_settlement": "not_verified",
            "case_settlement": "not_verified",
        }


async def validate_approved(db, tenant_id, tool_name, tool_input, proposal):
    from urllib.parse import urlsplit

    from app.services.chat.tools import parse_external_tool_name
    from app.services.chat.write_payload import normalize_write_payload
    from app.services.mcp_connector_service import get_mcp_connector

    parsed = parse_external_tool_name(tool_name)
    if not parsed or parsed[1] != "ns_updateRecord":
        return
    normalized = normalize_write_payload(tool_input)
    if not is_tax_update(str(tool_input.get("recordType", "")), normalized.fields):
        return
    parsed = parse_external_tool_name(tool_name)
    if (
        not proposal
        or proposal.get("tenant_id") != str(tenant_id)
        or not parsed
        or str(parsed[0]) != proposal["connector_id"]
        or normalized.record_id != proposal["record_id"]
        or normalized.fields != proposal["proposed_fields"]
        or normalized.lines
        or tool_input.get("recordType", "").lower() != proposal["record_type"]
    ):
        raise ValueError("This tax update needs a fresh, evidence-bound approval card.")
    connector = await get_mcp_connector(db, parsed[0], tenant_id)
    if (
        not connector
        or urlsplit(connector.server_url).hostname
        != f"{proposal['scope']['netsuite_account_id']}.suitetalk.api.netsuite.com"
    ):
        raise ValueError("The approved NetSuite connector/account binding changed.")
    await revalidate(db, tenant_id, proposal)


def approval_text(p):
    return (
        f"**Accounting correction for approval — {p['order_reference']}**\n\n"
        f"Invoice {p['record_id']}: total {p['before']['total']} → {p['expected_after']['total']}; "
        f"tax {p['before']['taxTotal']} → {p['expected_after']['taxTotal']} {p['source']['currency']}. "
        f"Ship-to country: {p['before'].get('shipCountry', 'not verified')}. "
        f"Tax agency: {(p['tax_item'].get('taxAgency') or {}).get('refName', 'not verified')}. "
        f"Tax account: {p['tax_account']} ({p.get('tax_account_name')}); "
        f"AR account: {p['ar_account']} ({p.get('ar_account_name')}); book: {p['accounting_book']}. "
        f"Posting period: {p['period'].get('periodName', p['period']['id'])}; "
        f"AR locked: {p['period']['arLocked']}; all locked: {p['period']['allLocked']}. "
        + p["approval_basis"]
        + "\n\nReview the exact change below. Nothing has been sent to NetSuite."
    )


async def candidate_confirmation(*, db, tenant_id, actor_id, correlation_id, session_id, task, tools, policy, case_id):
    """Present a supported correction through the existing HITL card, without another model hop."""
    import re

    from app.services.chat.mutation_guard import classify_connector_mutation
    from app.services.chat.write_confirmation_service import build_confirmation_payload
    from app.services.chat.write_payload import normalize_write_payload
    from app.services.chat.write_validation import validate_mutation
    from app.services.policy_service import evaluate_tool_call

    p = db.info.get("accounting_correction_candidate")
    # An evidence-only question is not a request to prepare a change.
    if not re.search(r"\b(?:prepare|propose|fix|correct|resolve|repair|update)\b", task, re.I):
        return None
    if re.search(r"\b(?:do not|don't|never)\s+(?:prepare|propose|show|create)\b", task, re.I):
        return None
    if not p or p.get("case_id") != case_id:
        return None
    name = f"ext__{p['connector_id'].replace('-', '')}__ns_updateRecord"
    params = {"recordType": p["record_type"], "recordId": p["record_id"], "data": json.dumps(p["proposed_fields"])}
    if name not in {t.get("name") for t in tools or []}:
        raise ValueError("The scoped NetSuite update tool is unavailable; no approval card was created.")
    review_for_card(db, tenant_id, name, p["record_type"], normalize_write_payload(params))
    if not evaluate_tool_call(policy, name, params)["allowed"]:
        raise ValueError("The configured policy blocks this correction.")
    if await classify_connector_mutation(name, db, tenant_id) != "update":
        raise ValueError("The scoped connector does not expose a verified update operation.")
    validation = await validate_mutation(
        tool_name=name,
        tool_input=params,
        mutation_type="update",
        record_type=p["record_type"],
        tenant_id=tenant_id,
        actor_id=actor_id,
        correlation_id=correlation_id,
        db=db,
        session_id=session_id,
    )
    if not validation.ok:
        raise ValueError("Native write validation needs review: " + json.dumps(validation.as_model_error()))
    card = build_confirmation_payload(
        mutation_type="update",
        record_type=p["record_type"],
        tool_name=name,
        tool_input=params,
        session_id=session_id,
        current_record=p["before"],
        validation=validation,
    )
    if card is None:
        raise ValueError("The verified invoice update could not be represented by an approval card.")
    card.accounting_review = p
    from app.services.audit_service import log_event

    await log_event(
        db,
        tenant_id,
        actor_id=actor_id,
        category="transaction_ops",
        action="accounting_correction.proposed",
        resource_type="transaction_case",
        resource_id=case_id,
        correlation_id=correlation_id,
        status="pending",
        payload={
            "session_id": session_id,
            "record_id": p["record_id"],
            "scope": p["scope"],
            "before": p["before"],
            "proposed_fields": p["proposed_fields"],
            "expected_after": p["expected_after"],
            "approval_basis": p["approval_basis"],
            "approval_required": True,
            "financial_writes": 0,
        },
    )
    return card, approval_text(p)
