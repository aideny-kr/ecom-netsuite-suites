"""Reallocate an existing credit's item lines, proposed by the agent and accepted by outcome.

The agent decides the treatment: which credit, which items, how much. This module never
trusts those numbers. It recomputes the order's posted balance with the proposal applied
and accepts it only when that balance equals the finalized source in gross, net and tax to
the cent, and every invariant below holds. A refusal carries a code and the numbers the
agent needs to correct itself; it is never a reason to try another write path.

What may change: the amounts and items of the credit's item lines, within its unchanged
total. Items are the credit's own or the subsidiary's configured tax-refund item
(``refund_adjustments.tax_item_accounts``). Header, applications, refund, period, customer
and exchange rate are never sent. Tax is classified by GL account, so it covers both a tax
added on top of the price (US) and a tax included in it (VAT/GST), without a rate.
"""

from decimal import Decimal, InvalidOperation

KIND = "credit_line_reallocation"


class RefusalError(ValueError):
    """A proposal the outcome check rejects. ``code`` is stable; ``detail`` is for the agent."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail or {}

    def __str__(self):
        return self.code


def _dec(value):
    if value is None or isinstance(value, (bool, float)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _amount(value, precision):
    """A positive amount at currency precision, from a string or int only (never a binary float)."""
    number = _dec(value)
    if number is None or number <= 0 or number != number.quantize(Decimal(1).scaleb(-precision)):
        raise RefusalError("invalid_amount", {"amount": str(value)})
    return number


def _ref(document, key):
    value = (document or {}).get(key)
    return str(value.get("id")) if isinstance(value, dict) and value.get("id") is not None else None


def _q(value, precision):
    return str(Decimal(value).quantize(Decimal(1).scaleb(-precision)))


def _tax_accounts(profile):
    return {str(a) for a in profile.get("tax_accounts") or ()} | {
        str(a) for a in (profile.get("tax_item_accounts") or {}).values()
    }


def _gl_rows(gl):
    rows = [r for r in (gl or {}).get("rows") or [] if r.get("debit") is not None or r.get("credit") is not None]
    if (gl or {}).get("complete") is not True or len({str(r.get("accountingbook")) for r in rows}) > 1:
        raise RefusalError("evidence_incomplete", {"reason": "gl_incomplete_or_multi_book"})
    return rows


def _posted(document, gl, taxed, sign):
    """(gross, tax) a document posts, as positive amounts: an invoice debits AR and credits tax
    (sign 1); a credit credits AR and debits tax (sign -1). Callers subtract credits."""
    ar = _ref(document, "account")
    gross = tax = Decimal(0)
    for row in _gl_rows(gl):
        debit, credit = _dec(row.get("debit")) or Decimal(0), _dec(row.get("credit")) or Decimal(0)
        account = str(row.get("account"))
        if account == ar:
            gross += (debit - credit) * sign
        elif account in taxed:
            tax += (credit - debit) * sign
    if gross != _dec(document.get("total")):
        raise RefusalError(
            "evidence_incomplete", {"reason": "gl_total_differs_from_header", "record_id": document.get("id")}
        )
    return gross, tax


def _balance(gross, tax, precision):
    return {"gross": _q(gross, precision), "net": _q(gross - tax, precision), "tax": _q(tax, precision)}


def _facts(*, credit, credit_gl, invoices, other_credits, source, profile, items, period, subsidiary_id, precision):
    """Everything about the order that does not depend on the proposal; refuses early and specifically."""
    documents = [credit, *(d for d, _ in invoices), *(d for d, _ in other_credits)]
    if (
        not invoices
        or (credit.get("line_evidence") or {}).get("complete") is not True
        or (credit.get("application_evidence") or {}).get("complete") is not True
        or not (credit.get("line_evidence") or {}).get("lines")
    ):
        raise RefusalError("evidence_incomplete", {"reason": "credit_lines_or_applications_incomplete"})
    invoice = invoices[0][0]
    if (
        len({_ref(d, "subsidiary") for d in documents} | {str(profile.get("subsidiary_id")), str(subsidiary_id)}) != 1
        or len({_ref(d, "entity") for d in documents}) != 1
        or len({_ref(d, "currency") for d in documents}) != 1
        or source.get("currency") != invoice.get("currency_code")
    ):
        raise RefusalError("credit_scope_mismatch")
    if any(_dec(d.get("exchangeRate")) != 1 for d in documents):
        raise RefusalError("foreign_currency_unsupported")
    if period.get("id") is not None and str(period["id"]) != _ref(credit, "postingPeriod"):
        raise RefusalError("evidence_incomplete", {"reason": "period_is_not_the_credits"})
    if any(period.get(flag) is not False for flag in ("closed", "arLocked", "allLocked")):
        raise RefusalError("period_locked", {"period_id": _ref(credit, "postingPeriod")})
    total, paid = _dec(source.get("total")), _dec(source.get("payment_total"))
    source_tax = _dec(source.get("tax_total"))
    if (
        source.get("state") != "complete"
        or source.get("payment_state") != "paid"
        or None in (total, paid, source_tax)
        or paid != total
    ):
        raise RefusalError("source_not_final")
    if (_dec(credit.get("taxTotal")) or Decimal(0)) != 0:
        raise RefusalError("credit_tax_engine_nonzero")
    taxed = _tax_accounts(profile)
    if not taxed:
        raise RefusalError("tax_accounts_not_configured")
    gross = tax = Decimal(0)
    for document, gl in invoices:
        g, t = _posted(document, gl, taxed, 1)
        gross, tax = gross + g, tax + t
    other_gross = other_tax = Decimal(0)
    for document, gl in other_credits:
        g, t = _posted(document, gl, taxed, -1)
        other_gross, other_tax = other_gross + g, other_tax + t
    credit_gross, credit_tax = _posted(credit, credit_gl, taxed, -1)
    before = (gross - other_gross - credit_gross, tax - other_tax - credit_tax)
    required = (total, source_tax)
    if before[0] != required[0]:
        raise RefusalError(
            "gross_not_reconciled",
            {"booked": _balance(*before, precision), "required": _balance(*required, precision)},
        )
    if before[1] == required[1]:
        raise RefusalError("no_difference", {"booked": _balance(*before, precision)})
    return {
        "taxed": taxed,
        "invoice_gross": gross,
        "invoice_tax": tax,
        "others": (other_gross, other_tax),
        "before": before,
        "required": required,
        "credit_total": _dec(credit.get("total")),
    }


def _lines(lines, credit, profile, items, taxed, precision):
    existing = {int(line["line"]): line for line in credit["line_evidence"]["lines"]}
    allowed = {_ref(line, "item") for line in existing.values()} | {
        str(k) for k in (profile.get("tax_item_accounts") or {})
    }
    seen, parsed = set(), []
    for raw in lines if isinstance(lines, list) else []:
        amount = _amount(raw.get("amount"), precision)
        item_id = str(raw.get("item_id") or "")
        number = raw.get("line")
        if number is not None:
            if isinstance(number, bool) or not isinstance(number, int) or number not in existing:
                raise RefusalError("unknown_line", {"line": number, "existing_lines": sorted(existing)})
            if number in seen:
                raise RefusalError("duplicate_line", {"line": number})
            seen.add(number)
            if _dec(existing[number].get("quantity")) != 1:
                raise RefusalError("line_quantity_unsupported", {"line": number})
        if item_id not in allowed:
            raise RefusalError("item_not_allowed", {"item_id": item_id, "allowed": sorted(i for i in allowed if i)})
        item = items.get(item_id) or {}
        if item.get("isInactive") is not False:
            raise RefusalError("item_inactive", {"item_id": item_id})
        account = _ref(item, "incomeAccount")
        configured = (profile.get("tax_item_accounts") or {}).get(item_id)
        if configured is not None and account != str(configured):
            raise RefusalError("tax_item_account_mismatch", {"item_id": item_id, "configured": str(configured)})
        if configured is None and account in taxed:
            raise RefusalError("item_not_allowed", {"item_id": item_id, "reason": "unconfigured_item_posts_to_tax"})
        parsed.append({"line": number, "item_id": item_id, "amount": amount, "account": account})
    if not parsed:
        raise RefusalError("invalid_amount", {"reason": "no_lines"})
    if seen != set(existing):
        raise RefusalError("existing_line_missing", {"missing": sorted(set(existing) - seen)})
    return parsed, existing


def assess(
    *, lines, credit, credit_gl, invoices, other_credits, source, profile, items, period, subsidiary_id, precision=2
):
    """Accept the agent's lines only if the order then equals the source; return the exact card data."""
    facts = _facts(
        credit=credit,
        credit_gl=credit_gl,
        invoices=invoices,
        other_credits=other_credits,
        source=source,
        profile=profile,
        items=items,
        period=period,
        subsidiary_id=subsidiary_id,
        precision=precision,
    )
    parsed, existing = _lines(lines, credit, profile, items, facts["taxed"], precision)
    total = sum((line["amount"] for line in parsed), Decimal(0))
    if total != facts["credit_total"]:
        raise RefusalError(
            "lines_total_changed",
            {"credit_total": _q(facts["credit_total"], precision), "lines_total": _q(total, precision)},
        )
    new_tax = sum((line["amount"] for line in parsed if line["account"] in facts["taxed"]), Decimal(0))
    other_gross, other_tax = facts["others"]
    after = (facts["invoice_gross"] - other_gross - total, facts["invoice_tax"] - other_tax - new_tax)
    if after != facts["required"]:
        raise RefusalError(
            "outcome_does_not_match_source",
            {"required": _balance(*facts["required"], precision), "proposed": _balance(*after, precision)},
        )
    template = existing[min(existing)]
    carried = {k: template[k] for k in ("isTaxable", "taxCode") if k in template}
    wire, debit = [], {}
    for line in parsed:
        amount = _q(line["amount"], precision)
        source_line = existing.get(line["line"], template)
        entry = {"item": {"id": line["item_id"]}, "quantity": 1, "rate": amount, "amount": amount}
        entry.update({k: source_line[k] for k in ("isTaxable", "taxCode") if k in source_line} or carried)
        if line["line"] is not None:
            entry = {"line": line["line"], **entry}
        wire.append(entry)
        debit[line["account"]] = debit.get(line["account"], Decimal(0)) + line["amount"]
    return {
        "kind": KIND,
        "proposed_fields": {"item": {"items": wire}},
        "expected_after": {"total": _q(total, precision), "taxTotal": _q(0, precision)},
        "expected_ledger": {
            "debit": {account: _q(value, precision) for account, value in sorted(debit.items())},
            "credit": {_ref(credit, "account"): _q(total, precision)},
        },
        "balance": {
            "before": _balance(*facts["before"], precision),
            "after": _balance(*after, precision),
            "source": _balance(*facts["required"], precision),
        },
    }


def derive(*, credit, credit_gl, invoices, other_credits, source, profile, items, period, subsidiary_id, precision=2):
    """The same fix computed by the server for a group member: the tax part of this credit is
    whatever the order's posted tax exceeds the source by. One existing line, one tax item."""
    facts = _facts(
        credit=credit,
        credit_gl=credit_gl,
        invoices=invoices,
        other_credits=other_credits,
        source=source,
        profile=profile,
        items=items,
        period=period,
        subsidiary_id=subsidiary_id,
        precision=precision,
    )
    tax_items = sorted(str(k) for k in (profile.get("tax_item_accounts") or {}))
    lines = credit["line_evidence"]["lines"]
    if len(tax_items) != 1 or len(lines) != 1:
        raise RefusalError("derive_unsupported_shape", {"tax_items": tax_items, "credit_lines": len(lines)})
    other_tax = facts["others"][1]
    tax_part = facts["invoice_tax"] - other_tax - facts["required"][1]
    net_part = facts["credit_total"] - tax_part
    if tax_part <= 0 or net_part < 0:
        raise RefusalError("derive_unsupported_shape", {"tax_part": _q(tax_part, precision)})
    line = int(lines[0]["line"])
    if net_part == 0:
        return [{"line": line, "item_id": tax_items[0], "amount": _q(tax_part, precision)}]
    return [
        {"line": line, "item_id": _ref(lines[0], "item"), "amount": _q(net_part, precision)},
        {"item_id": tax_items[0], "amount": _q(tax_part, precision)},
    ]
