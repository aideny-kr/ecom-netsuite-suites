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


def _lines(lines, profile, items, taxed, precision):
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
        parsed.append({"item_id": item_id, "amount": amount, "account": account})
    if not parsed:
        raise RefusalError("invalid_amount", {"reason": "no_lines"})
    return parsed


def assess(*, lines, memo, posting_date, **order):
    """Accept the agent's lines only if the order then equals the source; return the exact card data."""
    precision = order.get("precision", 2)
    found = facts(posting_date=posting_date, **order)
    parsed = _lines(lines, order["profile"], order["items"], found["taxed"], precision)
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
        "memo": memo_text[:MEMO_MAX],
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
