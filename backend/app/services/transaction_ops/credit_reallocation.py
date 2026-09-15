"""Existing-credit tax allocation: evidence and proposal construction only.

The credit's gross amount and refund/application remain unchanged. This pure
builder supplies intents to the separately configured native preview, signed
approval and dispatcher; it cannot itself issue a card or perform a write.
"""

from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal, localcontext

from app.services.transaction_ops.commercial_credits import _money
from app.services.transaction_ops.credit_classification import reference
from app.services.transaction_ops.line_evidence import (
    compare_source_lines,
    source_revision_delta,
    source_tax_refund_delta,
)
from app.services.transaction_ops.sales_credit import _gl_proves, _native_number

KIND = "credit_tax_reallocation"


def build_intent(tenant_id, case_id, source, review, evidence, support, *, field_map=None):
    """Construct a reviewable intent for an already-refunded price reduction.

    Requires complete order refund ownership, exact source repricing arithmetic,
    current native records and a balanced credit ledger. It retains the existing
    invoice's tax item/agency, book, AR and the credit's allowance item/account.
    Human review must establish source/tax authority; these observations alone
    never certify tax legality or authorize a posting.
    """
    from app.services.transaction_ops.accounting_field_map import resolve

    fields = resolve(field_map)
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            invoice, credit, refund = (support[k] for k in ("invoice", "credit", "refund"))
            scope = review["scope"]
            basis = source_revision_delta(
                source, evidence, invoice["id"], field_map=field_map
            ) or source_tax_refund_delta(source, evidence, invoice["id"])
            if not basis:
                return None
            gross, tax, net = (-_money(basis[k]) for k in ("gross_delta", "tax_delta", "net_delta"))
            changes = [
                c
                for c in compare_source_lines(source, evidence, field_map=field_map)["changes"]
                if str(c["target_record_id"]) == str(invoice["id"])
            ]
            if net > 0 and any(not c.get("tax_observation") for c in changes):
                return None
            if net > 0 and -sum((_money(c["tax_observation"]["delta"]) for c in changes), Decimal(0)) != tax:
                return None
            graph = support["refund_graph"]
            tax_item, item, period = (support[k] for k in ("tax_item", "item", "period"))
            ar, offset, tax_account, book = (
                str(support[k]) for k in ("ar_account", "offset_account", "tax_account", "book")
            )
            if (
                _money(source["total"]) <= 0
                or gross >= _money(invoice["total"])
                or (credit.get("taxTotal") is not None and _money(credit["taxTotal"]) != 0)
                or _money(credit["subtotal"]) != gross
                or any(_money(l["quantity"]) <= 0 or _money(l["price"]) < 0 for l in source["line_items"])
                or gross <= 0
                or tax <= 0
                or net < 0
                or gross != net + tax
                or review.get("configuration_status") != "scoped_configuration_found"
                or review.get("connection_active") is not True
                or (not review.get("native_mcp_connector_id") and not review.get("native_accounting_profile"))
                or source.get("payment_state") != "paid"
                or _money(source["payment_total"]) != _money(source["total"])
                or graph.get("complete") is not True
                or graph["refund_count"] != 1
                or graph["record_ids"] != [str(refund["id"])]
                or _money(graph["amount"]) != gross
                or len(graph["request_links"]) != 1
                or support["currency"]["symbol"] != source["currency"]
                or support["currency"]["currencyPrecision"] != 2
                or any(reference(d, "currency") != str(support["currency"]["id"]) for d in (invoice, credit, refund))
                or any(reference(d, "subsidiary") != str(scope["subsidiary_id"]) for d in (invoice, credit, refund))
                or any(_money(d["exchangeRate"]) != 1 for d in (invoice, credit, refund))
                or reference(credit, "entity") != reference(invoice, "entity")
                or credit.get(fields["order_reference"]) != source["number"]
                or _money(invoice["amountRemaining"]) != 0
                or _money(invoice["amountPaid"]) != _money(invoice["total"])
                or _money(credit["total"]) != gross
                or _money(credit["applied"]) != gross
                or _money(credit["unapplied"]) != 0
                or _money(refund["total"]) != gross
                or reference(credit, "account") != ar
                or reference(invoice, "account") != ar
                or len({ar, offset, tax_account}) != 3
                or not all(value.isdigit() for value in (ar, offset, tax_account, book))
                or credit.get("isTaxable") is not False
                or _money(credit["taxRate"]) != 0
                or item.get("isInactive") is not False
                or reference(item, "incomeAccount") != offset
                or tax_item.get("isInactive") is not False
                or str(tax_item["id"]) != reference(invoice, "taxItem")
                or not reference(tax_item, "taxAgency")
                or period.get("closed") is not False
                or period.get("arLocked") is not False
                or period.get("allLocked") is not False
                or str(period["id"]) != reference(credit, "postingPeriod")
                or not credit.get("lastModifiedDate")
            ):
                return None
            link = graph["request_links"][0]
            if (
                link.get("stage") != "refund_verified"
                or str(link.get("credit_memo_id")) != str(credit["id"])
                or str(link.get("refund_id")) != str(refund["id"])
                or _money(link["amount"]) != gross
            ):
                return None
            lines = credit["line_evidence"]
            if lines.get("complete") is not True or len(lines["lines"]) != 1:
                return None
            line = lines["lines"][0]
            if (
                reference(line, "item") != str(item["id"])
                or _money(line["quantity"]) != 1
                or _money(line["amount"]) != gross
                or _money(line["rate"]) != gross
                or line.get("isTaxable") is not False
                or not str(line["line"]).isdigit()
                or not line.get("lineUniqueKey")
                or not isinstance(line.get("itemType"), dict)
                or line["itemType"].get("id") != "NonInvtPart"
            ):
                return None
            # A missing native taxTotal is not defaulted to zero: the complete
            # balanced ledger must independently prove the current allocation.
            if not _gl_proves(support["credit_gl"], offset, ar, gross, book):
                return None
            invoice_gl = support["invoice_gl"]
            if invoice_gl.get("complete") is not True:
                return None
            rows = invoice_gl["rows"]
            if any(str(r["accountingbook"]) != book for r in rows):
                return None
            balances = {}
            for row in rows:
                debit, credit_amount = _money(row.get("debit") or 0), _money(row.get("credit") or 0)
                if debit < 0 or credit_amount < 0:
                    return None
                account = str(row.get("account"))
                if (debit or credit_amount) and not account.isdigit():
                    return None
                balances[account] = balances.get(account, Decimal(0)) + debit - credit_amount
            if sum(balances.values(), Decimal(0)) != 0 or balances.get(ar) != _money(invoice["total"]):
                return None
            invoice_tax = sum(
                (
                    _money(r.get("credit") or 0) - _money(r.get("debit") or 0)
                    for r in rows
                    if str(r.get("account")) == tax_account
                ),
                Decimal(0),
            )
            if invoice_tax != _money(invoice["taxTotal"]):
                return None
            rate = (tax / net * 100).quantize(Decimal(".0000001"), rounding=ROUND_HALF_UP) if net else None
            if rate is not None and (net * rate / 100).quantize(Decimal(".01"), rounding=ROUND_HALF_UP) != tax:
                return None
            # This is an effective integration allocation, not a newly selected
            # statutory rate. Source and tax authority remain explicit review work.
            return deepcopy(
                {
                    "kind": KIND,
                    "tenant_id": str(tenant_id),
                    "case_id": str(case_id),
                    "order_reference": source["number"],
                    "record_type": "creditmemo",
                    "record_id": str(credit["id"]),
                    "invoice_id": str(invoice["id"]),
                    "sales_order_id": reference(invoice, "createdFrom"),
                    "lock_record_type": "invoice",
                    "mutation_type": "update",
                    "connector_id": str(review["native_mcp_connector_id"])
                    if review.get("native_mcp_connector_id")
                    else None,
                    "connection_id": str(review["netsuite_connection_id"]),
                    "config_id": str(review["config_id"]),
                    "scope": scope,
                    "source": source,
                    "before": credit,
                    "support": support,
                    "source_revision_basis": basis,
                    "observed_at": support["observed_at"],
                    "accounting_book": book,
                    "ar_account": ar,
                    "sales_adjustment_account": offset,
                    "tax_account": tax_account,
                    "tax_item": tax_item,
                    "tax_agency": reference(tax_item, "taxAgency"),
                    "period": period,
                    "proposed_fields": {
                        "taxItem": {"id": str(tax_item["id"])},
                        **({"taxRate": str(rate)} if rate is not None else {"taxTotal": _native_number(tax)}),
                        "isTaxable": True,
                        "item": {
                            "items": [
                                {
                                    "line": line["line"],
                                    "rate": _native_number(net),
                                    "amount": _native_number(net),
                                    "isTaxable": True,
                                }
                            ]
                        },
                    },
                    "expected_after": {
                        "total": str(gross),
                        "subtotal": str(net),
                        "taxTotal": str(tax),
                        "applied": str(gross),
                        "unapplied": "0.00",
                    },
                    "expected_ledger": {"debit": {offset: str(net), tax_account: str(tax)}, "credit": {ar: str(gross)}},
                    "approval_basis": "Reallocate the existing credit between allowances and tax using the displayed "
                    "current source revision. Keep gross credit, refund, applications, AR, original period and invoice "
                    "unchanged. Finance must confirm the source reduction and tax basis, including any unfrozen "
                    "source adjustment, and retaining the displayed existing tax item/agency/accounts. "
                    "No additional credit, refund, cash movement, item-policy change "
                    "or invoice reduction is authorized. "
                    "Verify the exact ledger allocation and unchanged related records after execution. "
                    "Sales-order alignment remains a separately approved step.",
                    "status": "intent_requires_schema_policy_and_preflight_validation",
                    "required_transport": "native_accounting_amendment_with_tax_preview",
                    "tax_only": net == 0,
                    "financial_write_authorized": False,
                }
            )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


async def collect_support(db, tenant_id, source, review, evidence, *, field_map=None):
    """Complete the current credit/refund graph before proposing any reallocation.

    This is read-only discovery. A returned intent is deliberately not an
    executable candidate: the configured native preview and signed approval
    dispatcher must separately verify account policy and native behavior.
    """
    from datetime import datetime, timezone

    from app.services.transaction_ops.netsuite_reader import _id, authenticated_reader
    from app.services.transaction_ops.netsuite_refunds import collect_refunds

    sections = evidence.get("sections") or {}
    invoices = sections.get("posting_documents") or []
    if len(invoices) != 1 or invoices[0].get("record_type") != "invoice":
        return None
    invoice = invoices[0]
    basis = source_revision_delta(source, evidence, invoice.get("id"), field_map=field_map) or source_tax_refund_delta(
        source, evidence, invoice.get("id")
    )
    if not basis or _money(basis["net_delta"]) > 0 or any(_money(basis[k]) >= 0 for k in ("gross_delta", "tax_delta")):
        return None
    documents = (sections.get("related_refund_documents") or {}).get("documents") or []
    credits = [d for d in documents if str(d.get("record_type", "")).lower() == "creditmemo"]
    refunds = [d for d in documents if str(d.get("record_type", "")).lower() == "customerrefund"]
    if len(credits) != 1 or len(refunds) != 1:
        return None
    credit, refund = credits[0], refunds[0]
    invoice_gl = (sections.get("gl") or {}).get(str(invoice.get("id"))) or {}
    credit_gl = (sections.get("gl") or {}).get(str(credit.get("id"))) or {}
    if invoice_gl.get("complete") is not True or credit_gl.get("complete") is not True:
        return None
    rows = invoice_gl.get("rows") or []
    books = {str(r.get("accountingbook")) for r in rows}
    tax_rows = [
        r for r in rows if r.get("credit") is not None and _money(r["credit"]) == _money(invoice.get("taxTotal"))
    ]
    if len(books) != 1 or len(tax_rows) != 1:
        return None
    lines = (credit.get("line_evidence") or {}).get("lines") or []
    if len(lines) != 1 or (lines[0].get("itemType") or {}).get("id") != "NonInvtPart":
        return None
    scope = review.get("scope") or {}
    identifiers = {
        "order": reference(invoice, "createdFrom"),
        "currency": reference(invoice, "currency"),
        "item": reference(lines[0], "item"),
        "period": reference(credit, "postingPeriod"),
        "tax_item": reference(invoice, "taxItem"),
    }
    if not all(_id(v) for v in identifiers.values()):
        return None
    if (sections.get("sales_order") or {}).get("tranId") != source.get("number"):
        return None
    tax_items = [item for item in sections.get("taxItem", []) if str(item.get("id")) == identifiers["tax_item"]]
    if len(tax_items) != 1:
        return None
    async with authenticated_reader(
        db,
        tenant_id,
        review["netsuite_connection_id"],
        scope["netsuite_account_id"],
        max_api_calls=24,
    ) as reader:
        graph = await collect_refunds(
            reader,
            identifiers["order"],
            str(scope["subsidiary_id"]),
            identifiers["currency"],
            order_reference=source["number"],
            adjustment_profile=None,
        )
        currency = await reader.request("GET", f"/record/v1/currency/{identifiers['currency']}")
        item = await reader.request("GET", f"/record/v1/nonInventorySaleItem/{identifiers['item']}")
        period = await reader.request("GET", f"/record/v1/accountingPeriod/{identifiers['period']}")
    # collect_refunds raises on incomplete ownership/allocation. Do not mark a
    # historical lead complete merely because its credit has the same gross.
    return {
        "invoice": invoice,
        "credit": credit,
        "refund": refund,
        "currency": {k: currency[k] for k in ("id", "symbol", "currencyPrecision") if k in currency},
        "item": {k: item[k] for k in ("id", "isInactive", "incomeAccount") if k in item},
        "period": {k: period[k] for k in ("id", "closed", "arLocked", "allLocked") if k in period},
        "tax_item": tax_items[0],
        "ar_account": reference(invoice, "account"),
        "offset_account": reference(item, "incomeAccount"),
        "tax_account": str(tax_rows[0]["account"]),
        "book": next(iter(books)),
        "refund_graph": {**graph, "complete": True},
        "credit_gl": credit_gl,
        "invoice_gl": invoice_gl,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def solution_summary(intent):
    """Bounded, exact treatment for investigation; never implies a ready card."""
    return {
        "kind": KIND,
        "status": intent["status"],
        "record_type": intent["record_type"],
        "record_id": intent["record_id"],
        "invoice_id": intent["invoice_id"],
        "sales_order_id": intent["sales_order_id"],
        "currency": intent["source"]["currency"],
        "tax_only": intent["tax_only"],
        "required_transport": intent["required_transport"],
        "proposed_fields": intent["proposed_fields"],
        "expected_after": intent["expected_after"],
        "expected_ledger": intent["expected_ledger"],
        "approval_basis": intent["approval_basis"],
        "remaining_requirements": [
            "Verify account tax treatment and source authority.",
            "Enable the account-scoped native amendment connection and validate its unsaved tax preview.",
            "Obtain exact human approval after fresh source, ledger and application preflight.",
            "Prepare the separate sales-order alignment; retain the paid invoice and existing cash/refund records.",
        ],
        "executable": False,
        "financial_write_authorized": False,
    }
