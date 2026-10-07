"""Create a credit memo, proposed by the agent and accepted by outcome (smart resolver, slice 1).

The agent decides the fix: which items and how much. This module never trusts those numbers.
It recomputes the order's posted balance (invoices less credits, tax classified by GL account)
with the new credit applied, and accepts it only when that balance equals the finalized source
in gross, net and tax to the cent. There is no per-cause or per-label code: an unbooked
reseller discount (R231821517), a VAT refund and a missed promotion are the same check.

The same engine as ``credit_line_reallocation`` (which edits an existing credit's lines), for a
second shape: a new credit applied to the order's one invoice. A refusal carries a code and the
numbers the agent needs to correct itself. Spec: docs/superpowers/specs/2026-10-07-smart-resolver.md
"""

from decimal import Decimal

from app.services.transaction_ops.credit_line_reallocation import (
    NET_ACCOUNT_TYPES,
    RefusalError,
    _add,
    _adjustments,
    _amount,
    _balance,
    _dec,
    _posted,
    _q,
    _ref,
    _tax_accounts,
)

KIND = "credit_creation"
# A new credit may carry sales adjustments (Discount items) as well as non-inventory charges;
# inventory items would move stock, which a credit for a commercial difference must not do.
CREDIT_ITEM_TYPES = frozenset({"NonInvtPart", "OthCharge", "Service", "Discount"})
MEMO_MAX = 500
# The most credits one order's readback reads. A proposal needs room for its own credit
# within it, or a correct post could never be verified (review round 2).
MAX_CREDITS = 8


def _item_account(item):
    """The account an item posts to: Discount items carry ``account``, the others ``incomeAccount``."""
    return _ref(item, "incomeAccount") or _ref(item, "account")


def facts(
    *,
    invoices,
    credits,
    source,
    profile,
    items,
    period,
    posting_date,
    subsidiary_id,
    account_types=None,
    precision=2,
    require_difference=True,
):
    """Everything about the order that does not depend on the proposal; refuses early and specifically.

    ``require_difference=False`` is the readback: after the write the order must agree.
    """
    if len(invoices) != 1:
        raise RefusalError("invoice_count_unsupported", {"invoices": len(invoices)})
    invoice, _ = invoices[0]
    documents = [invoice, *(d for d, _ in credits)]
    if (
        len({_ref(d, "subsidiary") for d in documents} | {str(profile.get("subsidiary_id")), str(subsidiary_id)}) != 1
        or len({_ref(d, "entity") for d in documents}) != 1
        or len({_ref(d, "currency") for d in documents}) != 1
        or source.get("currency") != invoice.get("currency_code")
    ):
        raise RefusalError("credit_scope_mismatch")
    if any(_dec(d.get("exchangeRate")) != 1 for d in documents):
        raise RefusalError("foreign_currency_unsupported")
    if not period.get("id") or any(period.get(flag) is not False for flag in ("closed", "arLocked", "allLocked")):
        raise RefusalError("period_locked", {"period_id": period.get("id")})
    total, source_tax = _dec(source.get("total")), _dec(source.get("tax_total"))
    if (
        source.get("state") != "complete"
        or source.get("requires_review") not in (False, None)
        or not source.get("completed_at")
        or None in (total, source_tax)
        or any("finalized" in a and a["finalized"] is not True for a in _adjustments(source))
    ):
        raise RefusalError("source_not_final")
    if require_difference and len(credits) >= MAX_CREDITS:
        raise RefusalError("too_many_credits", {"credits": len(credits), "max_existing": MAX_CREDITS - 1})
    taxed = _tax_accounts(profile)
    gross, tax, invoice_by_account = _posted(invoice, invoices[0][1], taxed, 1, account_types)
    credit_gross = credit_tax = Decimal(0)
    credit_by_account = {}
    for document, gl in credits:
        g, t, by = _posted(document, gl, taxed, -1, account_types)
        credit_gross, credit_tax = credit_gross + g, credit_tax + t
        _add(credit_by_account, by)
    before = (gross - credit_gross, tax - credit_tax)
    required = (total, source_tax)
    if require_difference and before == required:
        raise RefusalError("no_difference", {"booked": _balance(*before, precision)})
    if require_difference and before[0] < required[0]:
        raise RefusalError(
            "netsuite_below_source",
            {"booked": _balance(*before, precision), "required": _balance(*required, precision)},
        )
    location = _ref(invoice, "location") or profile.get("correction_location_id")
    return {
        "taxed": taxed,
        "invoice": invoice,
        "before": before,
        "required": required,
        # What each tax account can still give back: the invoice's tax less earlier credits' reversals.
        "reversible": {a: v - credit_by_account.get(a, Decimal(0)) for a, v in invoice_by_account.items() if v > 0},
        "location": str(location) if location else None,
    }


def _lines(lines, profile, items, taxed, precision, account_types):
    configured = {str(k): str(v) for k, v in (profile.get("tax_item_accounts") or {}).items()}
    parsed = []
    for raw in lines if isinstance(lines, list) else []:
        if not isinstance(raw, dict):
            raise RefusalError("invalid_amount", {"reason": "line_not_an_object"})
        amount = _amount(raw.get("amount"), precision)
        item_id = str(raw.get("item_id") or "")
        item = items.get(item_id) or {}
        if not item:
            raise RefusalError("item_not_allowed", {"item_id": item_id, "reason": "unknown_item"})
        if item.get("isInactive") is not False:
            raise RefusalError("item_inactive", {"item_id": item_id})
        if item.get("itemType") not in CREDIT_ITEM_TYPES:
            raise RefusalError("item_not_allowed", {"item_id": item_id, "reason": "item_type"})
        account = _item_account(item)
        if not account:
            raise RefusalError("item_not_allowed", {"item_id": item_id, "reason": "no_posting_account"})
        if item_id in configured and account != configured[item_id]:
            raise RefusalError("tax_item_account_mismatch", {"item_id": item_id, "configured": configured[item_id]})
        if account in taxed and item_id not in configured:
            # Tax is reversed only through the subsidiary's configured tax-refund items.
            raise RefusalError("item_not_allowed", {"item_id": item_id, "reason": "unconfigured_item_posts_to_tax"})
        kind = (account_types or {}).get(account)
        if account not in taxed and kind not in NET_ACCOUNT_TYPES:
            # A non-tax line must post to an income account; an unknown or liability type is
            # never assumed to be a sales adjustment (smart resolver, review round 1).
            raise RefusalError("account_not_supported", {"item_id": item_id, "account": account, "type": kind})
        parsed.append({"item_id": item_id, "amount": amount, "account": account})
    if not parsed:
        raise RefusalError("invalid_amount", {"reason": "no_lines"})
    return parsed


def assess(*, lines, memo, posting_date, **order):
    """Accept the agent's lines only if the order then equals the source; return the exact card data."""
    precision = order.get("precision", 2)
    found = facts(posting_date=posting_date, **order)
    parsed = _lines(lines, order["profile"], order["items"], found["taxed"], precision, order.get("account_types"))
    total = sum((line["amount"] for line in parsed), Decimal(0))
    new_tax = sum((line["amount"] for line in parsed if line["account"] in found["taxed"]), Decimal(0))
    by_account = {}
    for line in parsed:
        if line["account"] in found["taxed"]:
            by_account[line["account"]] = by_account.get(line["account"], Decimal(0)) + line["amount"]
    for account, value in sorted(by_account.items()):
        if value > found["reversible"].get(account, Decimal(0)):
            raise RefusalError(
                "tax_reversal_exceeds_posted",
                {"account": account, "reversible": _q(found["reversible"].get(account, Decimal(0)), precision)},
            )
    after = (found["before"][0] - total, found["before"][1] - new_tax)
    if after != found["required"]:
        raise RefusalError(
            "outcome_does_not_match_source",
            {
                "before": _balance(*found["before"], precision),
                "required": _balance(*found["required"], precision),
                "proposed": _balance(*after, precision),
            },
        )
    invoice = found["invoice"]
    remaining = _dec(invoice.get("amountRemaining"))
    if remaining is None or remaining < total:
        raise RefusalError(
            "invoice_remaining_too_small",
            {"invoice_remaining": invoice.get("amountRemaining"), "credit": _q(total, precision)},
        )
    if not found["location"]:
        raise RefusalError("credit_location_required", {"subsidiary_id": str(order["subsidiary_id"])})
    reference = order["source"]["number"]
    text = " ".join(str(memo or "").split())
    memo_text = text if text.startswith(reference) else f"{reference} {text}".strip()
    debit = {}
    wire = []
    for line in parsed:
        amount = _q(line["amount"], precision)
        wire.append(
            {"item": {"id": line["item_id"]}, "quantity": 1, "rate": amount, "amount": amount, "isTaxable": False}
        )
        debit[line["account"]] = debit.get(line["account"], Decimal(0)) + line["amount"]
    ar = _ref(invoice, "account")
    proposed_fields = {
        "entity": {"id": _ref(invoice, "entity")},
        "subsidiary": {"id": _ref(invoice, "subsidiary")},
        "currency": {"id": _ref(invoice, "currency")},
        "account": {"id": ar},
        "location": {"id": found["location"]},
        "tranDate": posting_date,
        "postingPeriod": {"id": str(order["period"]["id"])},
        "memo": memo_text[:MEMO_MAX].rstrip(),  # stable when reassessed (review round 2)
        "autoApply": False,
        "toBeEmailed": False,
        "item": {"items": wire},
        "apply": {"items": [{"doc": {"id": str(invoice["id"])}, "apply": True, "amount": _q(total, precision)}]},
    }
    if _ref(invoice, "department"):
        proposed_fields["department"] = {"id": _ref(invoice, "department")}
    return {
        "kind": KIND,
        "proposed_fields": proposed_fields,
        "expected_after": {
            "total": _q(total, precision),
            "subtotal": _q(total - new_tax, precision),
            "taxTotal": _q(new_tax, precision),
        },
        "expected_ledger": {
            "debit": {account: _q(value, precision) for account, value in sorted(debit.items())},
            "credit": {ar: _q(total, precision)},
        },
        "balance": {
            "before": _balance(*found["before"], precision),
            "after": _balance(*after, precision),
            "source": _balance(*found["required"], precision),
        },
    }


def booked_balance(*, posting_date=None, **order):
    """The order's posted balance and the source's, as they stand now (the readback's view)."""
    precision = order.get("precision", 2)
    current = facts(posting_date=posting_date, require_difference=False, **order)
    return _balance(*current["before"], precision), _balance(*current["required"], precision)


# --- Reads, the card, approval and readback -------------------------------------------------
#
# The same gather() feeds the proposal, the approval-time revalidation and the readback, so the
# three can never check different things. No function here sends a write: the signed
# confirmation dispatcher behind the one-use permit owns execution. Mirrors
# credit_line_reallocation; the orchestration of reads is repeated (not the accounting logic,
# which is shared) so that path stays untouched. Unify the two readers in a follow-up.

READ_CALLS = 32
CARD_MAX_AGE_SECONDS = 300


def external_id(tenant_id, scope, case_id, invoice_id):
    """One stable identity per case and invoice: a second identical create is refused by NetSuite."""
    from app.services.transaction_ops.resolution_plan import fingerprint

    digest = fingerprint(
        {
            "tenant": str(tenant_id),
            "account": str(scope["netsuite_account_id"]),
            "subsidiary": str(scope["subsidiary_id"]),
            "case": str(case_id),
            "invoice": str(invoice_id),
            "kind": KIND,
        }
    )
    return f"ss-credit-{digest[:48]}"


async def gather(db, tenant_id, case_id, item_ids):
    """Fresh, complete evidence for a new credit on one case: ``(order, context)``.

    ``order`` is exactly what :func:`assess` takes besides ``lines`` and ``memo``. Read directly
    and independent of any cause: the sales order, its one invoice, every credit applied to or
    created from that invoice, every credit the refund graph reaches and every credit that names
    the order or carries this case's external ID; each with its GL. A credit that names the
    order but is not wholly applied to this invoice stops the proposal.
    """
    from datetime import datetime, timezone
    from uuid import UUID
    from zoneinfo import ZoneInfo

    from pydantic import ValidationError
    from sqlalchemy import select

    from app.models.transaction_ops import TransactionConfig
    from app.services.transaction_ops.accounting_review import accounting_context
    from app.services.transaction_ops.case_service import get_case
    from app.services.transaction_ops.credit_line_reallocation import _json, _suiteql, profile_matches_scope
    from app.services.transaction_ops.netsuite_reader import (
        HEADER_FIELDS,
        LINE_FIELDS,
        _id,
        _project,
        _sublist,
        authenticated_reader,
    )
    from app.services.transaction_ops.netsuite_refunds import collect_refunds
    from app.services.transaction_ops.periods import ReconciliationPolicy
    from app.services.transaction_ops.refund_adjustments import RefundAdjustmentProfile
    from app.services.transaction_ops.tax_correction import refresh_source

    case = await get_case(db, tenant_id, UUID(str(case_id)))
    review = await accounting_context(db, tenant_id, case.scope_json, case.latest_report_json)
    if (
        review.get("configuration_status") != "scoped_configuration_found"
        or review.get("connection_active") is not True
        or not review.get("native_mcp_connector_id")
    ):
        raise RefusalError("configuration_unavailable", {"status": review.get("configuration_status")})
    scope = review["scope"]
    subsidiary = str(scope["subsidiary_id"])
    config = await db.scalar(
        select(TransactionConfig).where(
            TransactionConfig.tenant_id == tenant_id, TransactionConfig.id == UUID(review["config_id"])
        )
    )
    mapping = (config and config.mapping_json) or {}
    try:
        profile = RefundAdjustmentProfile.model_validate(mapping["refund_adjustments"])
    except (KeyError, TypeError, ValidationError):
        raise RefusalError("tax_refund_items_not_configured", {"subsidiary_id": subsidiary}) from None
    if not profile_matches_scope(profile, scope):
        raise RefusalError("configuration_unavailable", {"reason": "profile_account_differs_from_case"})
    zone = ReconciliationPolicy.model_validate(mapping.get("reconciliation_policy") or {}).timezone_name
    posting_date = datetime.now(timezone.utc).astimezone(ZoneInfo(zone)).date().isoformat()
    source = _json(await refresh_source(db, tenant_id, scope, case.order_reference, include_accounting_detail=True))
    reference = str(source.get("number") or "")
    if reference != case.order_reference or not reference.replace("-", "").isalnum():
        raise RefusalError("evidence_incomplete", {"reason": "source_identity"})
    if (review.get("business_entity_subsidiaries") or {}).get(source.get("business_entity")) != subsidiary:
        # The source order must belong to this case's legal entity (as sales_credit and
        # normalization require): a matching reference elsewhere is never credited here.
        raise RefusalError("source_scope_mismatch", {"business_entity": source.get("business_entity")})
    wanted = sorted({str(i) for i in item_ids or ()} | {str(k) for k in profile.tax_item_accounts})
    if not wanted or not all(_id(i) for i in wanted):
        raise RefusalError("item_not_allowed", {"reason": "item_ids_unreadable"})
    ext = None
    async with authenticated_reader(
        db, tenant_id, review["netsuite_connection_id"], scope["netsuite_account_id"], max_api_calls=READ_CALLS
    ) as reader:

        async def ids(query, limit=50):
            return [str(r["id"]) for r in await _suiteql(reader, query, limit) if _id(str(r.get("id")))]

        orders = await _suiteql(
            reader,
            "SELECT t.id, t.tranid FROM transaction t JOIN transactionline tl ON tl.transaction = t.id "
            f"AND tl.mainline = 'T' WHERE t.type = 'SalesOrd' AND t.tranid = '{reference}' "
            f"AND tl.subsidiary = {subsidiary}",
            2,
        )
        if len(orders) != 1:
            raise RefusalError("evidence_incomplete", {"reason": "sales_order_not_unique", "found": len(orders)})
        raw_order = await reader.request(
            "GET", f"/record/v1/salesOrder/{orders[0]['id']}", params={"expandSubResources": "true"}
        )
        order_problems = []
        # The whole protected sales order (header, lines, revision), as credit_api_correction
        # protects it: any change between proposal, approval and readback is seen (round 3).
        order = {
            **_project(raw_order, HEADER_FIELDS),
            "lines": _sublist(raw_order, "item", "order", LINE_FIELDS, order_problems),
        }
        if order_problems or str(order.get("id")) != str(orders[0]["id"]) or order.get("tranId") != reference:
            raise RefusalError("evidence_incomplete", {"reason": "sales_order_unreadable"})
        order["id"] = str(order["id"])
        invoice_ids = await ids(
            "SELECT t.id FROM transaction t JOIN transactionline tl ON tl.transaction = t.id AND tl.mainline = 'T' "
            f"WHERE tl.createdfrom = {order['id']} AND t.type IN ('CustInvc', 'CashSale')"
        )
        if len(invoice_ids) != 1:
            raise RefusalError("invoice_count_unsupported", {"invoices": len(invoice_ids)})
        raw_invoice = await reader.request(
            "GET", f"/record/v1/invoice/{invoice_ids[0]}", params={"expandSubResources": "true"}
        )
        invoice = _project(
            raw_invoice, HEADER_FIELDS | {"account", "location", "department", "amountRemaining", "createdFrom"}
        )
        if (
            str(invoice.get("id")) != invoice_ids[0]
            or _ref(invoice, "subsidiary") != subsidiary
            or _ref(invoice, "createdFrom") != order["id"]
        ):
            raise RefusalError("evidence_incomplete", {"reason": "invoice_identity"})
        entity = _ref(invoice, "entity")
        if not _id(str(entity or "")):
            raise RefusalError("evidence_incomplete", {"reason": "invoice_entity"})
        ext = external_id(tenant_id, scope, case.id, invoice_ids[0])
        applied = await ids(
            "SELECT l.nextdoc AS id FROM nexttransactionlinelink l JOIN transaction t ON t.id = l.nextdoc "
            f"WHERE l.previousdoc = {invoice_ids[0]} AND l.linktype = 'Payment' AND t.type = 'CustCred'"
        )
        created = await ids(
            "SELECT t.id FROM transaction t JOIN transactionline tl ON tl.transaction = t.id AND tl.mainline = 'T' "
            f"WHERE tl.createdfrom = {invoice_ids[0]} AND t.type = 'CustCred'"
        )
        since = str(order.get("tranDate") or "")[:10]
        window = f"AND t.trandate >= TO_DATE('{since}', 'YYYY-MM-DD') " if len(since) == 10 else ""
        # Any customer in the subsidiary: a standalone credit naming this order under another
        # customer must still stop a second credit (review round 3).
        named = await ids(
            "SELECT t.id FROM transaction t JOIN transactionline tl ON tl.transaction = t.id AND tl.mainline = 'T' "
            f"WHERE t.type = 'CustCred' AND tl.subsidiary = {subsidiary} {window}"
            f"AND (t.memo LIKE '%{reference}%' OR t.externalid = '{ext}')"
        )
        try:
            graph = await collect_refunds(
                reader, order["id"], subsidiary, _ref(invoice, "currency") or "", order_reference=reference
            )
        except ValueError as exc:
            raise RefusalError("evidence_incomplete", {"reason": f"refund_graph:{exc}"}) from None
        manifest = graph.get("dependency_manifest") or {}
        graph_ids = [str(i) for i in manifest.get("transaction_ids") or [] if _id(str(i))]
        if manifest.get("truncated") is not False:
            raise RefusalError("evidence_incomplete", {"reason": "refund_graph_truncated"})
        graph_credits = (
            await ids(f"SELECT id FROM transaction WHERE type = 'CustCred' AND id IN ({','.join(graph_ids)})", 50)
            if graph_ids
            else []
        )
        credit_ids = sorted(set(applied) | set(created) | set(named) | set(graph_credits), key=int)
        if len(credit_ids) > MAX_CREDITS:
            raise RefusalError("evidence_incomplete", {"reason": "too_many_credits", "credits": len(credit_ids)})
        credits = []
        for ident in credit_ids:
            raw = await reader.request("GET", f"/record/v1/creditMemo/{ident}", params={"expandSubResources": "true"})
            doc = _project(raw, HEADER_FIELDS | {"account", "memo", "applied", "unapplied", "externalId"})
            problems = []
            lines = _sublist(raw, "item", "credit", LINE_FIELDS, problems)
            applications = _sublist(raw, "apply", "apply", frozenset({"doc", "apply", "amount"}), problems)
            doc["line_evidence"] = {"lines": lines or [], "complete": not problems}
            on_invoice = sum(
                (_dec(a.get("amount")) or Decimal(0))
                for a in applications or []
                if a.get("apply") is True and _ref(a, "doc") == invoice_ids[0]
            )
            if str(doc.get("id")) != ident or problems:
                raise RefusalError("evidence_incomplete", {"reason": "credit_unreadable", "credit": ident})
            if _dec(doc.get("unapplied")) != 0 or on_invoice != _dec(doc.get("total")):
                # A credit naming this order that is not wholly applied to its invoice: another
                # treatment (or a person) decides it, never a second credit beside it.
                raise RefusalError(
                    "existing_credit_not_applied_to_invoice", {"credit": ident, "tranId": doc.get("tranId")}
                )
            credits.append(doc)
        documents = [invoice_ids[0], *(c["id"] for c in credits)]
        gl_rows = await _suiteql(
            reader,
            "SELECT tal.transaction, tal.account, tal.accountingbook, tal.debit, tal.credit "
            f"FROM transactionaccountingline tal WHERE tal.transaction IN ({','.join(str(d) for d in documents)}) "
            "AND (tal.debit <> 0 OR tal.credit <> 0)",
            400,
        )
        gl = {str(d): {"complete": True, "rows": []} for d in documents}
        for row in gl_rows:
            gl[str(row["transaction"])]["rows"].append(
                {k: row.get(k) for k in ("account", "accountingbook", "debit", "credit")}
            )
        for section in gl.values():  # NetSuite returns rows in no fixed order (review round 3)
            section["rows"] = _canonical_gl(section)["rows"]
        account_ids = sorted({str(r["account"]) for r in gl_rows if r.get("account") is not None})
        if not account_ids or not all(_id(a) for a in account_ids):
            raise RefusalError("evidence_incomplete", {"reason": "gl_accounts_unreadable"})
        items = {
            str(r["id"]): {
                "id": str(r["id"]),
                "isInactive": r.get("isinactive") != "F",
                "itemType": r.get("itemtype"),
                "incomeAccount": {"id": str(r["incomeaccount"])} if r.get("incomeaccount") is not None else None,
            }
            for r in await _suiteql(
                reader, f"SELECT id, isinactive, incomeaccount, itemtype FROM item WHERE id IN ({','.join(wanted)})", 50
            )
        }
        # Every account a line could post to is typed, not only those already in the GL: a
        # proposed item's account of unknown or liability type must refuse before any write.
        account_ids = sorted(
            set(account_ids) | {a for item in items.values() if (a := _item_account(item)) and _id(a)}, key=int
        )
        account_types = {
            str(r["id"]): r.get("accttype")
            for r in await _suiteql(
                reader, f"SELECT id, accttype FROM account WHERE id IN ({','.join(account_ids)})", 100
            )
        }
        periods = await _suiteql(
            reader,
            "SELECT id FROM accountingperiod WHERE isyear='F' AND isquarter='F' AND isadjust='F' "
            f"AND startdate<=TO_DATE('{posting_date}','YYYY-MM-DD') "
            f"AND enddate>=TO_DATE('{posting_date}','YYYY-MM-DD')",
            2,
        )
        if len(periods) != 1 or not _id(str(periods[0].get("id"))):
            raise RefusalError("evidence_incomplete", {"reason": "posting_period_ambiguous"})
        period = await reader.request("GET", f"/record/v1/accountingPeriod/{periods[0]['id']}")
        currency = await reader.request("GET", f"/record/v1/currency/{_ref(invoice, 'currency')}")
        catalog = await reader.request("GET", "/record/v1/metadata-catalog/creditMemo")
    precision = currency.get("currencyPrecision")
    if isinstance(precision, bool) or not isinstance(precision, int) or not 0 <= precision <= 4:
        raise RefusalError("evidence_incomplete", {"reason": "currency_precision_unknown"})
    invoice["currency_code"] = currency.get("symbol")
    found = {
        "invoices": [(invoice, gl[str(invoice["id"])])],
        "credits": [(c, gl[str(c["id"])]) for c in credits],
        "source": source,
        "profile": {
            "subsidiary_id": profile.subsidiary_id,
            "tax_accounts": list(profile.tax_accounts),
            "tax_item_accounts": dict(profile.tax_item_accounts),
            "correction_location_id": profile.correction_location_id,
        },
        "account_types": account_types,
        "items": items,
        "period": {k: period.get(k) for k in ("id", "closed", "arLocked", "allLocked")},
        "posting_date": posting_date,
        "subsidiary_id": subsidiary,
        "precision": precision,
    }
    context = {
        "case_id": str(case.id),
        "report": case.latest_report_json or {},
        "review": review,
        "order": order,
        "catalog": catalog,
        "external_id": ext,
        "refund_graph": _json({k: graph.get(k) for k in ("amount", "record_ids", "dependency_manifest")}),
    }
    return found, context


def _canonical_gl(gl):
    """A GL section with its rows in one fixed order, so no comparison depends on how NetSuite
    happened to return them."""
    rows = (gl or {}).get("rows") or []
    return {
        "complete": (gl or {}).get("complete"),
        "rows": sorted(
            rows,
            key=lambda r: tuple(str(r.get(k)) for k in ("account", "accountingbook", "debit", "credit")),
        ),
    }


def baseline(found, context, *, exclude=None):
    """Everything the new credit must leave unchanged, in a form a correct write never changes:
    the invoice's identity and GL (not its open amount), every other credit, the profile and
    the sales order. Taken at proposal; recomputed at readback without the created credit."""
    from app.services.transaction_ops.resolution_plan import fingerprint

    invoice, invoice_gl = found["invoices"][0]
    return fingerprint(
        {
            "invoice": {
                k: invoice.get(k)
                for k in ("id", "tranId", "total", "entity", "subsidiary", "currency", "account", "createdFrom")
            },
            "invoice_gl": _canonical_gl(invoice_gl),
            # Every other credit's amounts AND ledger: an account change on an existing credit is a
            # side effect the readback must see (review round 2).
            "credits": sorted(
                [
                    {
                        **{k: str(d.get(k)) for k in ("id", "total", "applied", "unapplied", "externalId")},
                        "gl": sorted(
                            (
                                str(r.get("account")),
                                str(r.get("accountingbook")),
                                str(r.get("debit")),
                                str(r.get("credit")),
                            )
                            for r in (g or {}).get("rows") or []
                        ),
                    }
                    for d, g in found["credits"]
                    if str(d.get("id")) != str(exclude)
                ],
                key=lambda c: c["id"],
            ),
            "profile": found["profile"],
            "sales_order": context.get("order") or {},
        }
    )


def _identity(found, context):
    """What the approved write must leave unchanged: the order's invoices, existing credits, profile,
    sales order and refund graph. Checked at approval and readback."""
    from app.services.transaction_ops.resolution_plan import fingerprint

    return fingerprint(
        {
            "invoices": [(d, _canonical_gl(g)) for d, g in found["invoices"]],
            "credits": [(d, _canonical_gl(g)) for d, g in found["credits"]],
            "profile": found["profile"],
            "items": found["items"],
            "sales_order": context["order"],
            "refund_graph": context["refund_graph"],
        }
    )


def _proposal(tenant_id, found, context, result, lines, memo, reason):
    import json
    from datetime import datetime, timezone

    from app.services.transaction_ops.credit_api_correction import schema_contract, typed_fields
    from app.services.transaction_ops.credit_line_reallocation import _json

    review, source = context["review"], found["source"]
    invoice = found["invoices"][0][0]
    fields = {
        **result["proposed_fields"],
        "externalId": external_id(tenant_id, review["scope"], context["case_id"], invoice["id"]),
    }
    try:
        schema = schema_contract(context["catalog"], fields)
        wire = json.dumps(typed_fields(context["catalog"], fields), allow_nan=False)
    except ValueError as exc:
        raise RefusalError("connector_schema_unsupported", {"reason": str(exc)}) from None
    balance = result["balance"]
    lines_text = ", ".join(f"item {x['item']['id']} {x['amount']}" for x in fields["item"]["items"])
    return _json(
        {
            "kind": KIND,
            "mutation_type": "create",
            "tenant_id": str(tenant_id),
            "case_id": context["case_id"],
            "scope": review["scope"],
            "config_id": review["config_id"],
            "connection_id": review["netsuite_connection_id"],
            "connector_id": review["native_mcp_connector_id"],
            "order_reference": source["number"],
            "record_type": "creditmemo",
            "record_id": str(invoice["id"]),  # the document the credit is created against (lock key)
            "invoice_id": str(invoice["id"]),
            "sales_order_id": str(context["order"]["id"]),
            "reconciliation_target": {"record_type": "salesorder", "record_id": str(context["order"]["id"])},
            "lines": [{"item_id": x.get("item_id"), "amount": x.get("amount")} for x in lines],
            "memo": fields["memo"],
            "proposed_fields": fields,
            "wire_record_json": wire,
            "connector_schema": schema,
            "execution_transport": "mcp_record_api",
            "required_transport": "connected_mcp_record_create",
            "expected_after": result["expected_after"],
            "expected_ledger": result["expected_ledger"],
            "balance": balance,
            "source": source,
            "support": {
                "invoice": invoice,
                "identity": _identity(found, context),
                "baseline": baseline(found, context),
            },
            "protected_sales_order": context["order"],
            # The invoice as the human sees it before the credit (shown on the approval card).
            "before": {k: invoice.get(k) for k in ("tranId", "total", "amountRemaining", "subsidiary")},
            "accounting_book": next(
                (str(r.get("accountingbook")) for r in (found["invoices"][0][1] or {}).get("rows") or []), None
            ),
            "ar_account": fields["account"]["id"],
            "period": dict(found["period"]),
            "sales_adjustment_account": ",".join(
                sorted(a for a in result["expected_ledger"]["debit"] if a not in _tax_accounts(found["profile"]))
            ),
            "tax_account": ",".join(sorted(_tax_accounts(found["profile"]))),
            "reason": str(reason or "")[:500],
            "approval_basis": (
                f"Create a credit memo for {result['expected_after']['total']} {source.get('currency')} "
                f"({lines_text}) and apply it to invoice {invoice.get('tranId') or invoice['id']}. "
                f"Posted order balance after: net {balance['after']['net']}, tax {balance['after']['tax']}, "
                "which equals the finalized source. The items and amounts were chosen by the assistant from "
                "evidence; the server verified the outcome. The invoice, its existing credits and the sales "
                "order are not changed. The credit, its application and GL are re-read after execution."
            ),
            "status": "ready_for_exact_human_approval",
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    )


async def propose(db, tenant_id, case_id, lines, memo, reason):
    """Check the agent's lines against fresh evidence; on success, bind them as the case's candidate."""
    item_ids = (
        [str((x or {}).get("item_id") or "") for x in lines if isinstance(x, dict)] if isinstance(lines, list) else []
    )
    found, context = await gather(db, tenant_id, case_id, item_ids)
    result = assess(lines=lines, memo=memo, **found)
    proposal = _proposal(tenant_id, found, context, result, lines, memo, reason)
    from app.services.transaction_ops.resolution_plan import proposal_plan

    proposal["resolution_plan"] = proposal_plan(proposal, context["report"])
    db.info["accounting_correction_candidate"] = proposal
    return proposal


def review_for_card(db, tenant_id, tool_name, record_type, normalized, *, check_age=True):
    """Bind the model's ns_createRecord call to the exact server-verified proposal."""
    import json
    from datetime import datetime, timezone

    from app.services.chat.tools import parse_external_tool_name

    p = db.info.get("accounting_correction_candidate") or {}
    parsed = parse_external_tool_name(tool_name)
    if p.get("kind") != KIND:
        raise ValueError("fresh_credit_creation_proposal_required")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(p["observed_at"])).total_seconds()
    if (
        not parsed
        or parsed[1] != "ns_createRecord"
        or str(parsed[0]).replace("-", "") != str(p["connector_id"]).replace("-", "")
        or p["tenant_id"] != str(tenant_id)
        or record_type.lower() != "creditmemo"
        or normalized.record_id is not None
        or normalized.record != json.loads(p["wire_record_json"])
        or (check_age and not 0 <= age <= CARD_MAX_AGE_SECONDS)
    ):
        raise ValueError("credit_creation_binding_changed")
    return p


async def fresh(db, tenant_id, p):
    from app.services.transaction_ops.credit_api_correction import assert_binding_unchanged
    from app.services.transaction_ops.credit_line_reallocation import _json

    found, context = await gather(db, tenant_id, p["case_id"], [x["item_id"] for x in p["lines"]])
    assert_binding_unchanged(context["review"], p)
    if _json(found["source"]) != p["source"]:
        raise ValueError("credit_creation_source_changed")
    return found, context


async def validate_approved(db, tenant_id, tool_name, tool_input, p):
    """Immediately before the permit: the same check on fresh reads must give the same card."""
    import json

    from app.services.chat.write_payload import normalize_write_payload
    from app.services.transaction_ops import case_resolution_scope
    from app.services.transaction_ops.credit_api_correction import schema_contract, typed_fields

    db.info["accounting_correction_candidate"] = p
    review_for_card(
        db, tenant_id, tool_name, tool_input.get("recordType", ""), normalize_write_payload(tool_input), check_age=False
    )
    await case_resolution_scope.validate(db, tenant_id, p)
    found, context = await fresh(db, tenant_id, p)
    if _identity(found, context) != p["support"]["identity"]:
        raise ValueError("credit_creation_related_record_changed")
    try:
        result = assess(lines=p["lines"], memo=p["memo"], **found)
    except RefusalError as exc:
        raise ValueError(f"credit_creation_refused:{exc.code}") from None
    expected = {**result["proposed_fields"], "externalId": p["proposed_fields"]["externalId"]}
    if expected != p["proposed_fields"] or any(result[k] != p[k] for k in ("expected_after", "expected_ledger")):
        raise ValueError("credit_creation_treatment_changed")
    if schema_contract(context["catalog"], p["proposed_fields"]) != p["connector_schema"] or typed_fields(
        context["catalog"], p["proposed_fields"]
    ) != json.loads(p["wire_record_json"]):
        raise ValueError("credit_creation_schema_changed")


async def verify_after(db, tenant_id, p, receipt=None):
    """Readback: the created credit's lines, GL and application are the approved ones and the
    order now agrees with the source."""
    from app.services.transaction_ops.credit_line_reallocation import _ledger_matches

    try:
        found, context = await fresh(db, tenant_id, p)
        external = p["proposed_fields"]["externalId"]
        created = [(d, g) for d, g in found["credits"] if d.get("externalId") == external]
        if len(created) != 1:
            raise ValueError(f"credit_creation_not_found:{len(created)}")
        credit, credit_gl = created[0]
        if (
            str(found["invoices"][0][0].get("id")) != str(p["invoice_id"])
            or str((context.get("order") or {}).get("id")) != str(p.get("sales_order_id"))
            or baseline(found, context, exclude=credit["id"]) != p["support"]["baseline"]
        ):
            raise ValueError("credit_creation_related_record_changed")
        if isinstance(receipt, dict) and any(
            str(receipt[k]) != str(credit["id"]) for k in ("id", "recordId", "internalId") if receipt.get(k)
        ):
            raise ValueError("credit_creation_receipt_identity_conflict")
        approved = sorted((e["item"]["id"], _dec(e["amount"])) for e in p["proposed_fields"]["item"]["items"])
        saved_lines = (credit.get("line_evidence") or {}).get("lines") or []
        saved = sorted((_ref(line, "item"), _dec(line.get("amount"))) for line in saved_lines)
        if (credit.get("line_evidence") or {}).get("complete") is not True or saved != approved:
            raise ValueError("credit_creation_lines_differ")
        if not _ledger_matches(credit_gl, p["expected_ledger"]):
            raise ValueError("credit_creation_ledger_differs")
        total = _dec(p["expected_after"]["total"])
        if (
            _dec(credit.get("total")) != total
            or _dec(credit.get("applied")) != total
            or _dec(credit.get("unapplied")) != 0
        ):
            raise ValueError("credit_creation_not_fully_applied")
        if " ".join(str(credit.get("memo") or "").split()) != p["proposed_fields"]["memo"]:
            raise ValueError("credit_creation_memo_differs")
        booked, required = booked_balance(**found)
        if booked != required:
            raise ValueError("credit_creation_order_still_differs")
        return {
            "status": "verified",
            "record_type": "creditmemo",
            "record_id": str(credit["id"]),
            "credit_memo_id": str(credit["id"]),
            "credit_memo_number": credit.get("tranId"),
            "after": {"body": {"total": p["expected_after"]["total"], "memo": credit.get("memo")}, "lines": p["lines"]},
            "ledger": p["expected_ledger"],
            "balance": {"booked": booked, "source": required},
            "related_records_unchanged": True,
            "retry_allowed": False,
            "cash_settlement": "not_verified",
            "case_settlement": "not_verified",
        }
    except RefusalError as exc:
        return {"status": "needs_review", "reason": f"credit_creation_readback:{exc.code}", "retry_allowed": False}
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        return {"status": "needs_review", "reason": str(exc), "retry_allowed": False}


def project(p, report, current, *, verified_at, now):
    """The post-verification recheck: the order's posted balance (invoices less credits) decides
    whether the case reconciles, as for credit_line_reallocation."""
    from datetime import datetime, timedelta

    found, context = current
    if (
        str(found["invoices"][0][0]["id"]) != p["invoice_id"]
        or str(context["order"]["id"]) != p["sales_order_id"]
        or report.get("order_reference") != p["order_reference"]
    ):
        raise ValueError("credit_recheck_identity_changed")
    amounts = (report.get("balance") or {}).get("amounts") or {}
    if not all(isinstance(amounts.get(m), dict) and "source" in amounts[m] for m in ("order_total", "tax")):
        raise ValueError("credit_recheck_incomplete_report")
    source = found["source"]
    if Decimal(str(amounts["order_total"]["source"])) != Decimal(str(source["total"])) or Decimal(
        str(amounts["tax"]["source"])
    ) != Decimal(str(source["tax_total"])):
        raise ValueError("credit_recheck_source_changed")
    stamp = (report.get("source") or {}).get("observed_at")
    observed = datetime.fromisoformat(stamp) if stamp else None
    if not observed or not verified_at <= observed <= now or now - observed > timedelta(minutes=15):
        raise ValueError("credit_recheck_stale_evidence")
    booked, required = booked_balance(**found)

    def metric(key):
        delta = Decimal(required[key]) - Decimal(booked[key])
        return {"source": required[key], "target": booked[key], "delta": _q(delta, found["precision"])}

    posting = {
        "version": 1,
        "status": "matched" if booked == required else "difference",
        "basis": "verified_invoice_less_owned_credits",
        "currency": source.get("currency"),
        "amounts": {"order_total": metric("gross"), "tax": metric("tax"), "net": metric("net")},
        "records": {"invoice": p["invoice_id"], "creditmemo": p.get("credit_memo_id")},
    }
    refunds = amounts.get("refunds")
    refunds_agree = not refunds or Decimal(str(refunds.get("delta") or "0")) == 0
    status = "matched" if posting["status"] == "matched" and refunds_agree else "difference"
    return {
        **report,
        "balance": {
            **report["balance"],
            "status": status,
            "reason": "verified_invoice_less_created_credit",
            "amounts": {
                "order_total": posting["amounts"]["order_total"],
                "tax": posting["amounts"]["tax"],
                **({"refunds": refunds} if refunds else {}),
            },
            "missing_metrics": [
                m for m in (report["balance"].get("missing_metrics") or []) if m not in ("order_total", "tax")
            ],
            "original_order_comparison": report["balance"],
            "posting_reconciliation": posting,
        },
    }
