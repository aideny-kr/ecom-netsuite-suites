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

from decimal import Decimal

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
    """An exact Decimal by the platform's canonical rules (never a binary float), or None."""
    from app.schemas.transaction_ops import _decimal

    if value is None:
        return None
    try:
        return _decimal(value if isinstance(value, (Decimal, int, float, bool)) else str(value))
    except ValueError:
        return None


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
    """A document's posting rows, after the shared ledger reader has proven the section complete,
    single-book, non-negative and balanced."""
    from app.services.transaction_ops.posting_balance import _ledger as validated_ledger

    rows = [r for r in (gl or {}).get("rows") or [] if r.get("debit") is not None or r.get("credit") is not None]
    books = {str(r.get("accountingbook")) for r in (gl or {}).get("rows") or []}
    try:
        if len(books) != 1:
            raise ValueError("different_book")
        validated_ledger(gl or {}, books.pop())
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        raise RefusalError("evidence_incomplete", {"reason": f"gl:{exc}"}) from None
    return rows


# Allowlists, not denylists. A GL row counts as net only on an income account; any other account
# the subsidiary has not declared (a liability that may be tax, inventory/COGS, anything else) is a
# posting this check cannot account for, so it refuses. Only non-inventory items may be moved.
NET_ACCOUNT_TYPES = frozenset({"Income", "OthIncome"})
LINE_ITEM_TYPES = frozenset({"NonInvtPart", "OthCharge", "Service"})


def _posted(document, gl, taxed, sign, account_types):
    """(gross, tax, tax by account) a document posts, as positive amounts: an invoice debits AR and
    credits tax (sign 1); a credit credits AR and debits tax (sign -1). Callers subtract credits.
    Every other row must be on an account of known, non-liability, non-receivable type (net)."""
    ar = _ref(document, "account")
    gross = Decimal(0)
    by_account = {}
    for row in _gl_rows(gl):
        debit, credit = _dec(row.get("debit")) or Decimal(0), _dec(row.get("credit")) or Decimal(0)
        account = str(row.get("account"))
        if account == ar:
            gross += (debit - credit) * sign
        elif account in taxed:
            by_account[account] = by_account.get(account, Decimal(0)) + (credit - debit) * sign
        elif debit or credit:
            kind = (account_types or {}).get(account)
            if kind is None or kind == "AcctRec":
                raise RefusalError("evidence_incomplete", {"reason": "account_type_unknown", "account": account})
            if kind == "OthCurrLiab":
                raise RefusalError("tax_account_not_configured", {"account": account, "record_id": document.get("id")})
            if kind not in NET_ACCOUNT_TYPES:
                raise RefusalError(
                    "account_not_supported", {"account": account, "type": kind, "record_id": document.get("id")}
                )
    if gross != _dec(document.get("total")):
        raise RefusalError(
            "evidence_incomplete", {"reason": "gl_total_differs_from_header", "record_id": document.get("id")}
        )
    return gross, sum(by_account.values(), Decimal(0)), by_account


def _add(total, part):
    for account, value in part.items():
        total[account] = total.get(account, Decimal(0)) + value
    return total


def _adjustments(source):
    """Every source adjustment: order-level, line and shipment."""
    yield from source.get("adjustments") or []
    for owner in [*(source.get("line_items") or []), *(source.get("shipments") or [])]:
        yield from (owner or {}).get("adjustments") or []


def _balance(gross, tax, precision):
    return {"gross": _q(gross, precision), "net": _q(gross - tax, precision), "tax": _q(tax, precision)}


def _facts(
    *,
    credit,
    credit_gl,
    invoices,
    other_credits,
    source,
    profile,
    items,
    period,
    subsidiary_id,
    account_types=None,
    precision=2,
    require_difference=True,
):
    """Everything about the order that does not depend on the proposal; refuses early and specifically.

    ``require_difference=False`` is the readback: after the write the order must agree.
    """
    documents = [credit, *(d for d, _ in invoices), *(d for d, _ in other_credits)]
    if (
        not invoices
        or (credit.get("line_evidence") or {}).get("complete") is not True
        or (credit.get("application_evidence") or {}).get("complete") is not True
        or not (credit.get("line_evidence") or {}).get("lines")
    ):
        raise RefusalError("evidence_incomplete", {"reason": "credit_lines_or_applications_incomplete"})
    # The only credit this treatment edits: every line an item, the lines are the whole total (no
    # shipping/handling/discount on the side), and it is applied, i.e. it represents a settled refund.
    credit_lines = credit["line_evidence"]["lines"]
    if any(not _ref(line, "item") for line in credit_lines):
        raise RefusalError("credit_lines_unsupported", {"reason": "line_without_item"})
    if any((items.get(_ref(line, "item")) or {}).get("itemType") not in LINE_ITEM_TYPES for line in credit_lines):
        raise RefusalError("credit_lines_unsupported", {"reason": "inventory_or_unknown_item_type"})
    if sum((_dec(line.get("amount")) or Decimal(0) for line in credit_lines), Decimal(0)) != _dec(credit.get("total")):
        raise RefusalError("credit_has_non_item_charges")
    if _dec(credit.get("unapplied")) != 0 or _dec(credit.get("applied")) != _dec(credit.get("total")):
        raise RefusalError("credit_not_applied")
    # NetSuite's record API refuses any save of a credit without a location; such a credit gets the
    # subsidiary's configured correction location, stamped on the header (and so on every line).
    correction_location = None
    if not _ref(credit, "location"):
        correction_location = profile.get("correction_location_id")
        if not correction_location:
            raise RefusalError("credit_location_required", {"subsidiary_id": str(subsidiary_id)})
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
        or source.get("requires_review") not in (False, None)
        or not source.get("completed_at")
        or source.get("payment_state") != "paid"
        or None in (total, paid, source_tax)
        or paid != total
        or any("finalized" in a and a["finalized"] is not True for a in _adjustments(source))
    ):
        raise RefusalError("source_not_final")
    if (_dec(credit.get("taxTotal")) or Decimal(0)) != 0:
        raise RefusalError("credit_tax_engine_nonzero")
    taxed = _tax_accounts(profile)
    if not taxed:
        raise RefusalError("tax_accounts_not_configured")
    gross = tax = Decimal(0)
    invoice_by_account, other_by_account = {}, {}
    for document, gl in invoices:
        g, t, by = _posted(document, gl, taxed, 1, account_types)
        gross, tax = gross + g, tax + t
        _add(invoice_by_account, by)
    other_gross = other_tax = Decimal(0)
    for document, gl in other_credits:
        g, t, by = _posted(document, gl, taxed, -1, account_types)
        other_gross, other_tax = other_gross + g, other_tax + t
        _add(other_by_account, by)
    credit_gross, credit_tax, _ = _posted(credit, credit_gl, taxed, -1, account_types)
    before = (gross - other_gross - credit_gross, tax - other_tax - credit_tax)
    required = (total, source_tax)
    if before[0] != required[0]:
        raise RefusalError(
            "gross_not_reconciled",
            {"booked": _balance(*before, precision), "required": _balance(*required, precision)},
        )
    if require_difference and before[1] == required[1]:
        raise RefusalError("no_difference", {"booked": _balance(*before, precision)})
    return {
        "taxed": taxed,
        "invoice_gross": gross,
        "invoice_tax": tax,
        "others": (other_gross, other_tax),
        # What each tax account can still give back: invoice tax less other credits' reversals.
        "reversible": {a: v - other_by_account.get(a, Decimal(0)) for a, v in invoice_by_account.items() if v > 0},
        "before": before,
        "required": required,
        "credit_total": _dec(credit.get("total")),
        "correction_location": correction_location,
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
        if item.get("itemType") not in LINE_ITEM_TYPES:
            raise RefusalError("item_not_allowed", {"item_id": item_id, "reason": "item_type"})
        account = _ref(item, "incomeAccount")
        configured = (profile.get("tax_item_accounts") or {}).get(item_id)
        if configured is not None and account != str(configured):
            raise RefusalError("tax_item_account_mismatch", {"item_id": item_id, "configured": str(configured)})
        keeps_own_item = number is not None and _ref(existing[number], "item") == item_id
        if configured is None and account in taxed and not keeps_own_item:
            raise RefusalError("item_not_allowed", {"item_id": item_id, "reason": "unconfigured_item_posts_to_tax"})
        parsed.append({"line": number, "item_id": item_id, "amount": amount, "account": account})
    if not parsed:
        raise RefusalError("invalid_amount", {"reason": "no_lines"})
    if seen != set(existing):
        raise RefusalError("existing_line_missing", {"missing": sorted(set(existing) - seen)})
    return parsed, existing


def assess(
    *,
    lines,
    credit,
    credit_gl,
    invoices,
    other_credits,
    source,
    profile,
    items,
    period,
    subsidiary_id,
    account_types=None,
    precision=2,
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
        account_types=account_types,
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
    by_account = {}
    for line in parsed:
        if line["account"] in facts["taxed"]:
            by_account[line["account"]] = by_account.get(line["account"], Decimal(0)) + line["amount"]
    for account, value in sorted(by_account.items()):
        if account not in facts["reversible"]:
            raise RefusalError(
                "tax_account_not_on_invoice", {"account": account, "invoice_tax_accounts": sorted(facts["reversible"])}
            )
        if value > facts["reversible"][account]:
            raise RefusalError(
                "tax_reversal_exceeds_posted",
                {"account": account, "reversible": _q(facts["reversible"][account], precision)},
            )
    other_gross, other_tax = facts["others"]
    after = (facts["invoice_gross"] - other_gross - total, facts["invoice_tax"] - other_tax - new_tax)
    if after != facts["required"]:
        raise RefusalError(
            "outcome_does_not_match_source",
            {"required": _balance(*facts["required"], precision), "proposed": _balance(*after, precision)},
        )
    template = existing[min(existing)]
    wire, debit = [], {}
    for line in parsed:
        amount = _q(line["amount"], precision)
        source_line = existing.get(line["line"], template)
        entry = {"item": {"id": line["item_id"]}, "quantity": 1, "rate": amount, "amount": amount}
        for key in ("isTaxable", "taxCode"):  # each from the line itself, else from the template line
            if key in source_line:
                entry[key] = source_line[key]
            elif key in template:
                entry[key] = template[key]
        if line["line"] is not None:
            entry = {"line": line["line"], **entry}
        wire.append(entry)
        debit[line["account"]] = debit.get(line["account"], Decimal(0)) + line["amount"]
    proposed_fields = {"item": {"items": wire}}
    expected_after = {"total": _q(total, precision), "taxTotal": _q(0, precision)}
    if facts["correction_location"]:
        proposed_fields = {"location": {"id": str(facts["correction_location"])}, **proposed_fields}
        expected_after["location"] = str(facts["correction_location"])
    return {
        "kind": KIND,
        "proposed_fields": proposed_fields,
        "expected_after": expected_after,
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


def derive(
    *,
    credit,
    credit_gl,
    invoices,
    other_credits,
    source,
    profile,
    items,
    period,
    subsidiary_id,
    account_types=None,
    precision=2,
):
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
        account_types=account_types,
        precision=precision,
    )
    tax_items = sorted(
        str(item)
        for item, account in (profile.get("tax_item_accounts") or {}).items()
        if str(account) in facts["reversible"]
    )
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


def booked_balance(**facts):
    """The order's posted balance and the source's, as they stand now (the readback's view)."""
    precision = facts.get("precision", 2)
    current = _facts(**facts, require_difference=False)
    return _balance(*current["before"], precision), _balance(*current["required"], precision)


# --- Reads, the card, approval and readback -------------------------------------------------
#
# The same gather() feeds the proposal, the approval-time revalidation and the readback, so
# the three can never check different things. No function here sends a write: the signed
# confirmation dispatcher behind the one-use permit owns execution.

READ_CALLS = 32  # refund graph (<= 25) + transaction types + items + account types + period + currency + metadata
CARD_MAX_AGE_SECONDS = 300


def _json(value):
    """The one exact-decimal serializer (credit_api_correction._json), imported lazily: that module
    imports this one."""
    from app.services.transaction_ops import credit_api_correction

    return credit_api_correction._json(value)


async def _suiteql(reader, query, limit):
    from app.services.transaction_ops.netsuite_reader import _collection

    rows, complete = _collection(
        await reader.request("POST", "/query/v1/suiteql", params={"limit": limit + 1}, body={"q": query})
    )
    if not complete or len(rows) > limit:
        raise RefusalError("evidence_incomplete", {"reason": "suiteql_result_incomplete"})
    return rows


def profile_matches_scope(profile, scope):
    """As refund_adjustments.verified_tax_adjustments: a profile describes one NetSuite account and
    one subsidiary, and only that case scope may use it."""
    from app.services.transaction_ops.netsuite_reader import _account

    try:
        return profile.account_id == _account(scope["netsuite_account_id"]) and str(profile.subsidiary_id) == str(
            scope["subsidiary_id"]
        )
    except (KeyError, TypeError, ValueError):
        return False


async def gather(db, tenant_id, case_id, credit_memo_id):
    """Fresh, complete evidence for one credit of one case: ``(facts, context)``.

    ``facts`` is exactly what :func:`assess` and :func:`derive` take. Every invoice and credit
    the order's refund graph reaches must be in the evidence, or nothing is proposed.
    """
    from uuid import UUID

    from pydantic import ValidationError
    from sqlalchemy import select

    from app.models.transaction_ops import TransactionConfig
    from app.services.transaction_ops.accounting_evidence import collect_accounting_evidence
    from app.services.transaction_ops.accounting_review import accounting_context
    from app.services.transaction_ops.case_service import get_case
    from app.services.transaction_ops.netsuite_reader import _id, authenticated_reader
    from app.services.transaction_ops.netsuite_refunds import collect_refunds
    from app.services.transaction_ops.refund_adjustments import RefundAdjustmentProfile
    from app.services.transaction_ops.tax_correction import refresh_source

    if credit_memo_id is not None and not _id(str(credit_memo_id)):
        raise RefusalError("credit_not_in_case", {"credit_memo_id": str(credit_memo_id)})
    case = await get_case(db, tenant_id, UUID(str(case_id)))
    review = await accounting_context(db, tenant_id, case.scope_json, case.latest_report_json)
    if (
        review.get("configuration_status") != "scoped_configuration_found"
        or review.get("connection_active") is not True
        or not review.get("native_mcp_connector_id")
    ):
        raise RefusalError("configuration_unavailable", {"status": review.get("configuration_status")})
    scope = review["scope"]
    config = await db.scalar(
        select(TransactionConfig).where(
            TransactionConfig.tenant_id == tenant_id, TransactionConfig.id == UUID(review["config_id"])
        )
    )
    try:
        profile = RefundAdjustmentProfile.model_validate(((config and config.mapping_json) or {})["refund_adjustments"])
    except (KeyError, TypeError, ValidationError):
        raise RefusalError("tax_refund_items_not_configured", {"subsidiary_id": str(scope["subsidiary_id"])}) from None
    if not profile_matches_scope(profile, scope):
        raise RefusalError("configuration_unavailable", {"reason": "profile_account_differs_from_case"})
    source = _json(await refresh_source(db, tenant_id, scope, case.order_reference, include_accounting_detail=True))
    evidence = _json(await collect_accounting_evidence(db, tenant_id, review, case.latest_report_json))
    sections = evidence.get("sections") or {}
    order = sections.get("sales_order") or {}
    postings = sections.get("posting_documents") or []
    invoices = sorted((d for d in postings if d.get("record_type") == "invoice"), key=lambda d: int(d.get("id") or 0))
    if not _id(str(order.get("id"))) or order.get("tranId") != source.get("number") or len(invoices) != len(postings):
        raise RefusalError("evidence_incomplete", {"reason": "order_or_posting_documents_unsupported"})
    related = sections.get("related_refund_documents") or {}
    credits = [d for d in related.get("documents") or [] if str(d.get("record_type", "")).lower() == "creditmemo"]
    if credit_memo_id is None:
        # Group preparation names no credit: only an order with exactly one credit qualifies.
        target = credits[0] if len(credits) == 1 else None
    else:
        target = next((c for c in credits if str(c.get("id")) == str(credit_memo_id)), None)
    if target is None:
        raise RefusalError("credit_not_in_case", {"credit_memo_ids": [str(c.get("id")) for c in credits]})
    gl = sections.get("gl") or {}
    lines = (target.get("line_evidence") or {}).get("lines") or []
    if any(not _ref(line, "item") for line in lines):
        raise RefusalError("credit_lines_unsupported", {"reason": "line_without_item"})
    item_ids = sorted({_ref(line, "item") for line in lines} | {str(k) for k in profile.tax_item_accounts})
    if not all(_id(i) for i in item_ids) or not _id(_ref(target, "postingPeriod") or ""):
        raise RefusalError("evidence_incomplete", {"reason": "credit_references_unreadable"})
    account_ids = sorted(
        {
            str(row.get("account"))
            for document in [*invoices, *credits]
            for row in (gl.get(str(document.get("id"))) or {}).get("rows") or []
            if row.get("account") is not None
        }
    )
    if not account_ids or not all(_id(a) for a in account_ids):
        raise RefusalError("evidence_incomplete", {"reason": "gl_accounts_unreadable"})
    async with authenticated_reader(
        db, tenant_id, review["netsuite_connection_id"], scope["netsuite_account_id"], max_api_calls=READ_CALLS
    ) as reader:
        try:
            graph = await collect_refunds(
                reader,
                str(order["id"]),
                str(scope["subsidiary_id"]),
                _ref(invoices[0], "currency") if invoices else "",
                order_reference=source["number"],
            )
        except ValueError as exc:
            raise RefusalError("evidence_incomplete", {"reason": f"refund_graph:{exc}"}) from None
        manifest = graph.get("dependency_manifest") or {}
        ids = [str(i) for i in manifest.get("transaction_ids") or [] if _id(str(i))]
        if manifest.get("truncated") is not False or not ids:
            raise RefusalError("evidence_incomplete", {"reason": "refund_graph_truncated"})
        types = {
            str(r["id"]): r.get("type")
            for r in await _suiteql(reader, f"SELECT id, type FROM transaction WHERE id IN ({','.join(ids)})", 200)
        }
        if {i for i, t in types.items() if t == "CustInvc"} != {str(d["id"]) for d in invoices} or {
            i for i, t in types.items() if t == "CustCred"
        } != {str(c["id"]) for c in credits}:
            raise RefusalError("evidence_incomplete", {"reason": "order_documents_differ_from_refund_graph"})
        items = {
            str(r["id"]): {
                "id": str(r["id"]),
                "isInactive": r.get("isinactive") != "F",
                "itemType": r.get("itemtype"),
                "incomeAccount": {"id": str(r["incomeaccount"])} if r.get("incomeaccount") is not None else None,
            }
            for r in await _suiteql(
                reader,
                f"SELECT id, isinactive, incomeaccount, itemtype FROM item WHERE id IN ({','.join(item_ids)})",
                50,
            )
        }
        account_types = {
            str(r["id"]): r.get("accttype")
            for r in await _suiteql(
                reader, f"SELECT id, accttype FROM account WHERE id IN ({','.join(account_ids)})", 100
            )
        }
        period = await reader.request("GET", f"/record/v1/accountingPeriod/{_ref(target, 'postingPeriod')}")
        currency = await reader.request("GET", f"/record/v1/currency/{_ref(target, 'currency')}")
        catalog = await reader.request("GET", "/record/v1/metadata-catalog/creditMemo")
    precision = currency.get("currencyPrecision")
    if isinstance(precision, bool) or not isinstance(precision, int) or not 0 <= precision <= 4:
        raise RefusalError("evidence_incomplete", {"reason": "currency_precision_unknown"})
    facts = {
        "credit": target,
        "credit_gl": gl.get(str(target["id"])),
        "invoices": [(d, gl.get(str(d["id"]))) for d in invoices],
        "other_credits": [(c, gl.get(str(c["id"]))) for c in credits if c is not target],
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
        "subsidiary_id": str(scope["subsidiary_id"]),
        "precision": precision,
    }
    context = {
        "case_id": str(case.id),
        "report": case.latest_report_json or {},
        "review": review,
        "order": order,
        "catalog": catalog,
        "refund_graph": _json({k: graph.get(k) for k in ("amount", "record_ids", "dependency_manifest")}),
    }
    return facts, context


def _identity(facts, context):
    """What the approved write must leave unchanged: everything except the credit's own lines, its
    GL and the item set those lines use. Checked at approval, at readback and at the recheck, so
    a correct write never fails it and any other change does."""
    from app.services.transaction_ops.resolution_plan import fingerprint

    credit = facts["credit"]
    return fingerprint(
        {
            "credit": {
                k: credit.get(k)
                for k in (
                    "id",
                    "entity",
                    "account",
                    "subsidiary",
                    "currency",
                    "postingPeriod",
                    "tranDate",
                    "total",
                    "department",
                    "class",
                )
            },
            "applications": credit.get("application_evidence"),
            "invoices": [(d, g) for d, g in facts["invoices"]],
            "other_credits": [(d, g) for d, g in facts["other_credits"]],
            "profile": facts["profile"],
            "sales_order": context["order"],
            "refund_graph": context["refund_graph"],
        }
    )


def _preimage(facts, context):
    """What the human approved changing FROM: the credit's current lines, GL and the items they and
    the proposal use. Any change between proposal and approval invalidates the card."""
    from app.services.transaction_ops.resolution_plan import fingerprint

    lines = (facts["credit"].get("line_evidence") or {}).get("lines") or []
    return fingerprint(
        {
            "identity": _identity(facts, context),
            "lines": [
                {k: line.get(k) for k in ("line", "item", "quantity", "rate", "amount", "taxCode", "isTaxable")}
                for line in lines
            ],
            "credit_gl": facts["credit_gl"],
            "items": facts["items"],
            "location": facts["credit"].get("location"),
        }
    )


def _proposal(tenant_id, facts, context, result, lines, reason):
    import json
    from datetime import datetime, timezone

    from app.services.transaction_ops.credit_api_correction import schema_contract, typed_fields

    review, credit, source = context["review"], facts["credit"], facts["source"]
    invoice = facts["invoices"][0][0]
    try:
        schema = schema_contract(context["catalog"], result["proposed_fields"])
        wire = json.dumps(typed_fields(context["catalog"], result["proposed_fields"]), allow_nan=False)
    except ValueError as exc:
        raise RefusalError("connector_schema_unsupported", {"reason": str(exc)}) from None
    before_lines = [
        {"line": int(line["line"]), "item_id": _ref(line, "item"), "amount": str(line.get("amount"))}
        for line in credit["line_evidence"]["lines"]
    ]
    after = ", ".join(
        f"{line.get('line', 'new')}: item {line['item']['id']} {line['amount']}"
        for line in result["proposed_fields"]["item"]["items"]
    )
    balance = result["balance"]
    return _json(
        {
            "kind": KIND,
            "tenant_id": str(tenant_id),
            "case_id": context["case_id"],
            "scope": review["scope"],
            "config_id": review["config_id"],
            "connection_id": review["netsuite_connection_id"],
            "connector_id": review["native_mcp_connector_id"],
            "order_reference": source["number"],
            "record_type": "creditmemo",
            "record_id": str(credit["id"]),
            "invoice_id": str(invoice["id"]),
            "sales_order_id": str(context["order"]["id"]),
            "reconciliation_target": {"record_type": "salesorder", "record_id": str(context["order"]["id"])},
            "lines": [
                {
                    k: v
                    for k, v in {"line": x.get("line"), "item_id": x.get("item_id"), "amount": x.get("amount")}.items()
                    if v is not None
                }
                for x in lines
            ],
            "proposed_fields": result["proposed_fields"],
            "wire_record_json": wire,
            "connector_schema": schema,
            "execution_transport": "mcp_record_api",
            "required_transport": "connected_mcp_record_update",
            "expected_after": result["expected_after"],
            "expected_ledger": result["expected_ledger"],
            "balance": balance,
            "before": {"total": str(credit.get("total")), "taxTotal": "0.00", "lines": before_lines},
            "source": source,
            "support": {
                "invoice": invoice,
                "credit": credit,
                "identity": _identity(facts, context),
                "preimage": _preimage(facts, context),
            },
            "protected_sales_order": context["order"],
            "ar_account": _ref(credit, "account"),
            "tax_account": ",".join(sorted(_tax_accounts(facts["profile"]))),
            "reason": str(reason or "")[:500],
            "approval_basis": (
                f"Reallocate the lines of existing credit {credit.get('tranId') or credit['id']} "
                f"(total {credit.get('total')} {source.get('currency')} unchanged) to: {after}. "
                f"Posted order balance after: net {balance['after']['net']}, tax {balance['after']['tax']}, "
                f"which equals the finalized source. The credit's refund, applications, customer, period, "
                "invoice and sales order are not changed. The line items were chosen by the assistant from "
                "evidence and the subsidiary's configured tax-refund item; the server verified the result. "
                "The GL is re-read after execution."
            ),
            "status": "ready_for_exact_human_approval",
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    )


async def propose(db, tenant_id, case_id, credit_memo_id, lines, reason):
    """Check the agent's lines against fresh evidence; on success, bind them as the case's candidate."""
    facts, context = await gather(db, tenant_id, case_id, credit_memo_id)
    result = assess(lines=lines, **facts)
    proposal = _proposal(tenant_id, facts, context, result, lines, reason)
    from app.services.transaction_ops.resolution_plan import proposal_plan

    proposal["resolution_plan"] = proposal_plan(proposal, context["report"])
    db.info["accounting_correction_candidate"] = proposal
    return proposal


def review_for_card(db, tenant_id, tool_name, record_type, normalized, *, check_age=True):
    """Bind the model's ns_updateRecord call to the exact server-verified proposal."""
    import json
    from datetime import datetime, timezone

    from app.services.chat.tools import parse_external_tool_name

    p = db.info.get("accounting_correction_candidate") or {}
    parsed = parse_external_tool_name(tool_name)
    if p.get("kind") != KIND:
        raise ValueError("fresh_credit_reallocation_proposal_required")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(p["observed_at"])).total_seconds()
    if (
        not parsed
        or parsed[1] != "ns_updateRecord"
        or str(parsed[0]).replace("-", "") != str(p["connector_id"]).replace("-", "")
        or p["tenant_id"] != str(tenant_id)
        or record_type.lower() != "creditmemo"
        or normalized.record_id != p["record_id"]
        or normalized.record != json.loads(p["wire_record_json"])
        or (check_age and not 0 <= age <= CARD_MAX_AGE_SECONDS)
    ):
        raise ValueError("credit_reallocation_binding_changed")
    return p


async def fresh(db, tenant_id, p):
    from app.services.transaction_ops.credit_api_correction import assert_binding_unchanged

    facts, context = await gather(db, tenant_id, p["case_id"], p["record_id"])
    assert_binding_unchanged(context["review"], p)
    if _json(facts["source"]) != p["source"]:
        raise ValueError("credit_reallocation_source_changed")
    return facts, context


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
    facts, context = await fresh(db, tenant_id, p)
    if _preimage(facts, context) != p["support"]["preimage"]:
        raise ValueError("credit_reallocation_related_record_changed")
    try:
        result = assess(lines=p["lines"], **facts)
    except RefusalError as exc:
        raise ValueError(f"credit_reallocation_refused:{exc.code}") from None
    if any(result[k] != p[k] for k in ("proposed_fields", "expected_after", "expected_ledger")):
        raise ValueError("credit_reallocation_treatment_changed")
    if schema_contract(context["catalog"], p["proposed_fields"]) != p["connector_schema"] or typed_fields(
        context["catalog"], p["proposed_fields"]
    ) != json.loads(p["wire_record_json"]):
        raise ValueError("credit_reallocation_schema_changed")


def _ledger(gl):
    """The credit's saved GL by side and account, exact (never rounded before comparing)."""
    debit, credit = {}, {}
    for row in _gl_rows(gl):
        for side, bucket in (("debit", debit), ("credit", credit)):
            value = _dec(row.get(side))
            if value:
                account = str(row.get("account"))
                bucket[account] = bucket.get(account, Decimal(0)) + value
    return {"debit": debit, "credit": credit}


def _ledger_matches(gl, expected):
    saved = _ledger(gl)
    return all(
        set(saved[side]) == set(expected[side])
        and all(saved[side][a] == Decimal(expected[side][a]) for a in expected[side])
        for side in ("debit", "credit")
    )


def _lines_match(credit, p):
    """The saved lines are the approved ones, compared by content: NetSuite may renumber a sublist
    on save, and the exact GL comparison already pins which accounts received what."""
    lines = (credit.get("line_evidence") or {}).get("lines") or []
    if any(_dec(line.get("quantity")) != 1 for line in lines):
        return False
    approved_entries = p["proposed_fields"]["item"]["items"]
    # The tax fields the approval set are compared too: same item and amount under another tax code
    # is not the approved save.
    tax_keys = [k for k in ("taxCode", "isTaxable") if any(k in entry for entry in approved_entries)]

    def key(line, item, amount):
        tax = tuple((line.get(k) or {}).get("id") if isinstance(line.get(k), dict) else line.get(k) for k in tax_keys)
        return (item, amount, tax)

    saved = sorted(key(line, _ref(line, "item"), _dec(line.get("amount"))) for line in lines)
    approved = sorted(key(entry, entry["item"]["id"], Decimal(entry["amount"])) for entry in approved_entries)
    return saved == approved


async def verify_after(db, tenant_id, p, receipt=None):
    """Readback: the credit's saved lines and GL are the approved ones and the order now agrees."""
    try:
        if isinstance(receipt, dict) and any(
            str(receipt[k]) != p["record_id"] for k in ("id", "recordId", "internalId") if receipt.get(k)
        ):
            raise ValueError("credit_reallocation_receipt_identity_conflict")
        facts, context = await fresh(db, tenant_id, p)
        credit = facts["credit"]
        if _identity(facts, context) != p["support"]["identity"]:
            raise ValueError("credit_reallocation_related_record_changed")
        if credit.get("application_evidence") != p["support"]["credit"].get("application_evidence"):
            raise ValueError("credit_reallocation_applications_changed")
        if not _lines_match(credit, p):
            raise ValueError("credit_reallocation_lines_differ")
        if not _ledger_matches(facts["credit_gl"], p["expected_ledger"]):
            raise ValueError("credit_reallocation_ledger_differs")
        expected_location = p["expected_after"].get("location") or _ref(p["support"]["credit"], "location")
        if _ref(credit, "location") != expected_location:
            raise ValueError("credit_reallocation_location_differs")
        if (_dec(credit.get("taxTotal")) or Decimal(0)) != 0:
            raise ValueError("credit_reallocation_tax_engine_posted")
        ledger = {
            side: {a: str(v) for a, v in accounts.items()} for side, accounts in _ledger(facts["credit_gl"]).items()
        }
        booked, required = booked_balance(**facts)
        if booked != required:
            raise ValueError("credit_reallocation_order_still_differs")
        return {
            "status": "verified",
            "record_type": "creditmemo",
            "record_id": p["record_id"],
            "credit_memo_id": p["record_id"],
            "after": {"body": {"total": credit.get("total"), "taxtotal": "0.00"}, "lines": p["lines"]},
            "ledger": ledger,
            "balance": {"booked": booked, "source": required},
            "related_records_unchanged": True,
            "retry_allowed": False,
            "cash_settlement": "not_verified",
            "case_settlement": "not_verified",
        }
    except RefusalError as exc:
        return {"status": "needs_review", "reason": f"credit_reallocation_readback:{exc.code}", "retry_allowed": False}
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        return {"status": "needs_review", "reason": str(exc), "retry_allowed": False}


def project(p, report, current, *, verified_at, now):
    """The post-verification recheck: the order's posted balance (invoices less credits), not the
    unchanged sales order, decides whether the case reconciles."""
    from datetime import datetime, timedelta

    facts, context = current
    if (
        str(facts["credit"]["id"]) != p["record_id"]
        or str(facts["invoices"][0][0]["id"]) != p["invoice_id"]
        or str(context["order"]["id"]) != p["sales_order_id"]
        or report.get("order_reference") != p["order_reference"]
        or _identity(facts, context) != p["support"]["identity"]
    ):
        raise ValueError("credit_recheck_identity_changed")
    amounts = (report.get("balance") or {}).get("amounts") or {}
    if not all(isinstance(amounts.get(m), dict) and "source" in amounts[m] for m in ("order_total", "tax")):
        raise ValueError("credit_recheck_incomplete_report")
    source = facts["source"]
    if Decimal(str(amounts["order_total"]["source"])) != Decimal(str(source["total"])) or Decimal(
        str(amounts["tax"]["source"])
    ) != Decimal(str(source["tax_total"])):
        raise ValueError("credit_recheck_source_changed")
    stamp = (report.get("source") or {}).get("observed_at")
    observed = datetime.fromisoformat(stamp) if stamp else None
    # As accounting_credit_recheck.project: the report must describe NetSuite after the write,
    # observed within the last 15 minutes (this module's facts are read live by fresh()).
    if not observed or not verified_at <= observed <= now or now - observed > timedelta(minutes=15):
        raise ValueError("credit_recheck_stale_evidence")
    booked, required = booked_balance(**facts)

    def metric(key):
        delta = Decimal(required[key]) - Decimal(booked[key])
        return {"source": required[key], "target": booked[key], "delta": _q(delta, facts["precision"])}

    posting = {
        "version": 1,
        "status": "matched" if booked == required else "difference",
        "basis": "verified_invoice_less_owned_credits",
        "currency": source.get("currency"),
        "amounts": {"order_total": metric("gross"), "tax": metric("tax"), "net": metric("net")},
        "records": {"invoice": p["invoice_id"], "creditmemo": p["record_id"]},
    }
    refunds = amounts.get("refunds")
    refunds_agree = not refunds or Decimal(str(refunds.get("delta") or "0")) == 0
    status = "matched" if posting["status"] == "matched" and refunds_agree else "difference"
    return {
        **report,
        "balance": {
            **report["balance"],
            "status": status,
            "reason": "verified_invoice_less_existing_credit",
            "amounts": {
                "order_total": posting["amounts"]["order_total"],
                "tax": posting["amounts"]["tax"],
                **({"refunds": refunds} if refunds else {}),
            },
            # Only the two metrics this projection re-derived are no longer missing.
            "missing_metrics": [
                m for m in (report["balance"].get("missing_metrics") or []) if m not in ("order_total", "tax")
            ],
            "original_order_comparison": report["balance"],
            "posting_reconciliation": posting,
        },
    }


async def verified_exemplar(db, tenant_id, config_id):
    """The latest approved, independently verified correction of this kind on the same configuration.

    A group is never the first use of this treatment for a configuration: one case is proposed,
    approved and verified first, then members of the same shape can follow.
    """
    from sqlalchemy import select

    from app.models.chat import ChatMessage

    so = ChatMessage.structured_output
    return await db.scalar(
        select(ChatMessage.id)
        .where(
            ChatMessage.tenant_id == tenant_id,
            so["status"].astext == "approved",
            so["accounting_review"]["kind"].astext == KIND,
            so["accounting_review"]["config_id"].astext == str(config_id),
            so["accounting_verification"]["status"].astext == "verified",
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(1)
    )


async def prepare_group_member(db, tenant_id, case_id):
    """Group preparation: the same treatment, derived from this member's own figures by the server
    (no model arithmetic) and accepted by the same outcome check. Sets the case's candidate."""
    from app.services.transaction_ops.resolution_plan import proposal_plan

    facts, context = await gather(db, tenant_id, case_id, None)
    exemplar = await verified_exemplar(db, tenant_id, context["review"]["config_id"])
    if exemplar is None:
        raise RefusalError("no_verified_exemplar", {"config_id": context["review"]["config_id"]})
    lines = derive(**facts)
    result = assess(lines=lines, **facts)
    proposal = _proposal(
        tenant_id,
        facts,
        context,
        result,
        lines,
        f"Same treatment as verified correction {exemplar}, derived from this order's own source and GL.",
    )
    proposal["exemplar_confirmation_id"] = str(exemplar)
    proposal["resolution_plan"] = proposal_plan(proposal, context["report"])
    db.info["accounting_correction_candidate"] = proposal
    return proposal
