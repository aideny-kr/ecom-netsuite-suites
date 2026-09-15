"""Read-only financial position for proven source revisions and existing credits.

This projection never changes a case verdict, approves a treatment, or treats an
arbitrary refund as a reduction of the source order. The revision, owned refund
chain, currency and balanced ledgers must all agree first.
"""

from decimal import Decimal, localcontext

from app.services.transaction_ops.accounting_field_map import resolve
from app.services.transaction_ops.commercial_credits import _money
from app.services.transaction_ops.credit_classification import reference
from app.services.transaction_ops.line_evidence import source_revision_delta, source_tax_refund_delta


def _ledger(section, book):
    if section.get("complete") is not True or not section.get("rows"):
        raise ValueError("incomplete_ledger")
    balances = {}
    for row in section["rows"]:
        if str(row["accountingbook"]) != book:
            raise ValueError("different_book")
        debit, credit = (_money(row.get(k) if row.get(k) is not None else "0") for k in ("debit", "credit"))
        account = str(row.get("account"))
        if debit < 0 or credit < 0 or ((debit or credit) and not account.isdigit()):
            raise ValueError("invalid_ledger")
        balances[account] = balances.get(account, Decimal(0)) + debit - credit
    if sum(balances.values(), Decimal(0)) != 0:
        raise ValueError("unbalanced_ledger")
    return balances


def _metric(source, target):
    return {"source": str(source), "target": str(target), "delta": str(source - target)}


def repriced_credit_balance(source, review, evidence, support, report, *, field_map=None):
    """Separate invoice-minus-credit economics from non-posting order alignment.

    Period locks and execution availability do not change an observed balance.
    An already corrected credit also qualifies; a second credit is never implied.
    """
    if not support:
        return None
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            invoice, credit, refund = (support[k] for k in ("invoice", "credit", "refund"))
            order = evidence["sections"]["sales_order"]
            scope = review["scope"]
            basis = source_revision_delta(
                source, evidence, invoice["id"], field_map=field_map
            ) or source_tax_refund_delta(source, evidence, invoice["id"])
            if not basis:
                return None
            gross = -_money(basis["gross_delta"])
            graph = support["refund_graph"]
            source_refunds = report["refund_evidence"]["source"]
            if (
                gross <= 0
                or gross >= _money(invoice["total"])
                or _money(basis["net_delta"]) > 0
                or _money(basis["tax_delta"]) >= 0
                or reference(invoice, "createdFrom") != str(order["id"])
                or any(reference(d, "subsidiary") != str(scope["subsidiary_id"]) for d in (invoice, credit, refund))
                or any(reference(d, "currency") != str(support["currency"]["id"]) for d in (invoice, credit, refund))
                or support["currency"]["symbol"] != source["currency"]
                or any(_money(d["exchangeRate"]) != 1 for d in (invoice, credit, refund))
                or not reference(invoice, "entity")
                or reference(credit, "entity") != reference(invoice, "entity")
                or credit.get(resolve(field_map)["order_reference"]) != source["number"]
                or _money(invoice["amountRemaining"]) != 0
                or _money(invoice["amountPaid"]) != _money(invoice["total"])
                or any(_money(d["total"]) != gross for d in (credit, refund))
                or _money(credit["applied"]) != gross
                or _money(credit["unapplied"]) != 0
                or graph.get("complete") is not True
                or graph["refund_count"] != 1
                or graph["record_ids"] != [str(refund["id"])]
                or _money(graph["amount"]) != gross
                or len(graph["request_links"]) != 1
                or source_refunds.get("complete") is not True
                or source_refunds.get("events_complete") is not True
                or source_refunds["currency"] != source["currency"]
                or source_refunds["order_reference"] != source["number"]
                or _money(source_refunds["amount"]) != gross
                or source_refunds["refund_count"] != 1
                or len(source_refunds["events"]) != 1
            ):
                return None
            link, event = graph["request_links"][0], source_refunds["events"][0]
            if (
                link.get("stage") != "refund_verified"
                or str(link["credit_memo_id"]) != str(credit["id"])
                or str(link["refund_id"]) != str(refund["id"])
                or str(link["source_refund_id"]) != str(event["id"])
                or not event.get("payment_number")
                or link["payment_number"] != event["payment_number"]
                or _money(event["amount"]) != gross
                or _money(link["amount"]) != gross
            ):
                return None
            ar, offset, tax, book = (str(support[k]) for k in ("ar_account", "offset_account", "tax_account", "book"))
            if len({ar, offset, tax}) != 3 or not all(v.isdigit() for v in (ar, offset, tax, book)):
                return None
            invoice_gl, credit_gl = (_ledger(support[k], book) for k in ("invoice_gl", "credit_gl"))
            inv_total, inv_tax = _money(invoice["total"]), _money(invoice["taxTotal"])
            credit_net, credit_tax = credit_gl.get(offset, Decimal(0)), credit_gl.get(tax, Decimal(0))
            if (
                reference(invoice, "account") != ar
                or reference(credit, "account") != ar
                or invoice_gl.get(ar) != inv_total
                or -invoice_gl.get(tax, Decimal(0)) != inv_tax
                or credit_gl.get(ar) != -gross
                or any(amount for account, amount in credit_gl.items() if account not in {ar, offset, tax})
                or credit_net < 0
                or credit_tax < 0
                or credit_net + credit_tax != gross
                or _money(credit["subtotal"]) != credit_net
                or (credit.get("taxTotal") is not None and _money(credit["taxTotal"]) != credit_tax)
            ):
                return None
            total, source_tax = _money(source["total"]), _money(source["tax_total"])
            amounts = {
                "net": _metric(total - source_tax, inv_total - inv_tax - credit_net),
                "tax": _metric(source_tax, inv_tax - credit_tax),
                "order_total": _metric(total, inv_total - gross),
                "refunds": _metric(_money(source_refunds["amount"]), _money(graph["amount"])),
            }
            alignment = {
                "order_total": _metric(total, _money(order["total"])),
                "tax": _metric(source_tax, _money(order["taxTotal"])),
            }
            return {
                "version": 1,
                "status": "difference" if any(_money(v["delta"]) for v in amounts.values()) else "matched",
                "basis": "verified_source_revision_and_owned_credit_refund",
                "currency": source["currency"],
                "amounts": amounts,
                "sales_order_alignment": {
                    "status": "required" if any(_money(v["delta"]) for v in alignment.values()) else "matched",
                    "record_id": str(order["id"]),
                    "amounts": alignment,
                },
                "records": {
                    "invoice": str(invoice["id"]),
                    "creditmemo": str(credit["id"]),
                    "customerrefund": str(refund["id"]),
                },
                "observed_at": support["observed_at"],
                "source_refunds_observed_at": source_refunds.get("observed_at"),
                "accounting_book": book,
                "source_revision": source["updated_at"],
                "interpretation": "Invoice less the owned existing credit. Sales-order alignment is separate. "
                "This is observed accounting evidence, not tax-policy approval or cash settlement certification.",
            }
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None
