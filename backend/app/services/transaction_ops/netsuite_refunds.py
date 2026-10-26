"""Bounded native refund allocation graph; credits are never counted as returned money.

Credit memos created from the order's invoice are reported separately, as
`invoice_credits`, so the order comparison can count them (decided 2026-09-30:
Framework books a Solidus order adjustment as a credit memo created from the
invoice). Credit memos created from the order's own return authorization are
reported apart from them, as `invoice_credits.return_credits` (C15): a fully
returned order is credited from its return, which no invoice credit covers. Both
stay out of the refund amount: a credit is not returned money.
"""

import asyncio
import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from app.schemas.transaction_ops import _decimal
from app.services.transaction_ops.credit_line_reallocation import NET_ACCOUNT_TYPES
from app.services.transaction_ops.netsuite_reader import (
    REFUND_QUERY_READ_TIMEOUT_SECONDS,
    _account,
    _collection,
    _id,
    _sublist,
    authenticated_reader,
)
from app.services.transaction_ops.netsuite_refund_requests import (
    read_request_links,
    read_tagged_credits,
    verify_request_allocations,
)
from app.services.transaction_ops.refund_adjustments import RefundAdjustmentProfile, read_tax_adjustments

# +1 for the batched credit-memo ledger read, +1 for the invoice-credit totals read, +1 for the tagged-credit
# read and +1 for the reverse request check it makes for an order with no linked request (C09). The order's
# return credits ride the invoice-credit totals read (C15), so they need no call of their own.
MAX_REFUND_CALLS = 28
MAX_INVOICE_CREDITS = 10
# One read returns each credit's header and lines and each invoice's header.
MAX_INVOICE_CREDIT_ROWS = 200
# The rows of return credits no invoice links to, read in the same call; counted apart, so they
# never use up the invoice credits' rows.
MAX_RETURN_CREDIT_ROWS = 200
INVENTORY_COST_TYPES = frozenset({"COGS", "OthCurrAsset"})
MAX_DOCUMENTS = 100
MAX_EDGES = 200
MAX_DEPTH = 6
MAX_DEPENDENCIES = 600
_PARENTS = {
    "CustDep": {"SalesOrd"},
    "CustInvc": {"SalesOrd"},
    "CashSale": {"SalesOrd"},
    "RtnAuth": {"SalesOrd", "CustInvc", "CashSale"},
    "CustCred": {"SalesOrd", "CustInvc", "CashSale", "RtnAuth"},
    "DepAppl": {"CustDep"},
    "CashRfnd": {"CashSale", "RtnAuth"},
}
_REFUND_TYPES = {"CustRfnd": "customerrefund", "CashRfnd": "cashrefund"}
# Return authorizations that can produce no further credit or refund: Refunded (G) and Closed (H).
SETTLED_RETURN_STATUSES = frozenset({"G", "H"})


def _dict(value):
    if not isinstance(value, dict):
        raise ValueError("refund_evidence_invalid")
    return value


async def read_netsuite_refunds(
    db,
    tenant_id,
    connection_id,
    account_id,
    subsidiary_id,
    order_reference,
    target_evidence,
    *,
    client=None,
    adjustment_profile=None,
):
    account = _account(account_id)
    order, currency_id, currency = refund_scope(
        connection_id, account, subsidiary_id, order_reference, target_evidence, adjustment_profile
    )
    async with asyncio.timeout(160):
        async with authenticated_reader(
            db,
            tenant_id,
            connection_id,
            account,
            client=client,
            max_api_calls=MAX_REFUND_CALLS,
            query_read_timeout_seconds=REFUND_QUERY_READ_TIMEOUT_SECONDS,
        ) as reader:
            result = await collect_refunds(
                reader,
                order["record_id"],
                subsidiary_id,
                currency_id,
                order_reference=order_reference,
                adjustment_profile=adjustment_profile,
            )
            return {
                **result,
                "amount": str(result["amount"]),
                "complete": True,
                "provider": "netsuite",
                "account_id": account,
                "subsidiary_id": subsidiary_id,
                "connection_id": str(connection_id),
                "order_reference": order_reference,
                "currency": currency,
                "api_calls": reader.calls,
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }


# NetSuite sales order statuses with nothing left to fulfil or bill: Cancelled (C) and Closed (H).
CLOSED_ORDER_STATUSES = frozenset({"C", "H"})


def order_status(header):
    """The sales order's status code, as normalization reads it; None when absent (never an error)."""
    value = (header.get("orderStatus") or header.get("status")) if isinstance(header, dict) else None
    return value.get("id") if isinstance(value, dict) else None


def refund_scope(connection_id, account_id, subsidiary_id, order_reference, target_evidence, adjustment_profile=None):
    """Shared identity gate for native single and bulk collection."""
    account = _account(account_id)
    if adjustment_profile and RefundAdjustmentProfile.model_validate(adjustment_profile).account_id != account:
        raise ValueError("adjustment_profile_scope_mismatch")
    evidence = _dict(target_evidence)
    scope = _dict(evidence.get("scope"))
    lookup = _dict(evidence.get("lookup"))
    orders = evidence.get("orders")
    if (
        evidence.get("provider") != "netsuite"
        or scope.get("account_id") != account
        or scope.get("connection_id") != str(connection_id)
        or scope.get("subsidiary_id") != subsidiary_id
        or lookup.get("complete") is not True
        or lookup.get("count") != 1
        or not isinstance(orders, list)
        or len(orders) != 1
    ):
        raise ValueError("refund_target_scope_unproven")
    order = _dict(orders[0])
    header = _dict(order.get("header"))
    currency_id = _id(_dict(header.get("currency")).get("id"))
    currency = _dict(order.get("currency_metadata")).get("symbol")
    if (
        order.get("header_complete") is not True
        or order.get("order_reference") != order_reference
        or _id(header.get("id")) != _id(order.get("record_id"))
        or _dict(header.get("subsidiary")).get("id") != subsidiary_id
        or not isinstance(currency, str)
        or not re.fullmatch(r"[A-Z]{3}", currency)
    ):
        raise ValueError("refund_target_identity_unproven")
    return order, currency_id, currency


def _query(frontier):
    identifiers = ",".join(sorted(frontier, key=int))
    return (
        "SELECT DISTINCT l.previousdoc, l.nextdoc, p.type AS previoustype, n.type AS nexttype, "
        "p.currency AS previouscurrency, n.currency AS nextcurrency, "
        "pl.subsidiary AS previoussubsidiary, nl.subsidiary AS nextsubsidiary, "
        "p.posting AS previousposting, n.posting AS nextposting, "
        "p.voided AS previousvoided, n.voided AS nextvoided, "
        # A document's own status and total, read in the same query: what reconciliation needs to
        # see that a canceled order's deposits are all fully applied, at no extra call (2026-10-22).
        "p.status AS previousstatus, n.status AS nextstatus, "
        "p.foreigntotal AS previoustotal, n.foreigntotal AS nexttotal "
        "FROM NextTransactionLink l JOIN transaction p ON p.id=l.previousdoc "
        "JOIN transaction n ON n.id=l.nextdoc "
        "LEFT JOIN transactionline pl ON pl.transaction=p.id AND pl.mainline='T' "
        "LEFT JOIN transactionline nl ON nl.transaction=n.id AND nl.mainline='T' "
        f"WHERE l.previousdoc IN ({identifiers}) OR l.nextdoc IN ({identifiers}) "
        "ORDER BY l.previousdoc, l.nextdoc"
    )


def _positive(value):
    amount = _decimal(value)
    if amount is None or amount <= 0:
        raise ValueError("invalid_refund_amount")
    return amount


def invoice_credit_candidates(nodes, parents):
    """Non-voided credit memos linked from an invoice: created from it, or only applied to it.

    The graph cannot tell those two apart; the totals read checks `createdfrom`.
    """
    return sorted(
        (
            identifier
            for identifier, metadata in nodes.items()
            if metadata.get("type") == "CustCred"
            and metadata.get("voided") != "T"
            and any((nodes.get(p) or {}).get("type") == "CustInvc" for p in parents.get(identifier, ()))
        ),
        key=int,
    )


def return_credit_candidates(nodes, parents, reachable):
    """Non-voided credit memos linked from a return authorization the order reaches (C15).

    The totals read checks each one's `createdfrom` and whether the order owns it.
    """
    return sorted(
        (
            identifier
            for identifier in reachable
            if (nodes.get(identifier) or {}).get("type") == "CustCred"
            and nodes[identifier].get("voided") != "T"
            and any(
                p in reachable and (nodes.get(p) or {}).get("type") == "RtnAuth" for p in parents.get(identifier, ())
            )
        ),
        key=int,
    )


def billing_documents(nodes, reachable):
    """The order's billing documents: every non-voided invoice or cash sale downstream of it."""
    return sorted(
        (
            identifier
            for identifier in reachable
            if (nodes.get(identifier) or {}).get("type") in {"CustInvc", "CashSale"}
            and nodes[identifier].get("voided") != "T"
        ),
        key=int,
    )


def _posting(lines, total, tax, taxed):
    """ "income" when a credit's lines prove it a sales reduction, else "unproven".

    Every line other than the native tax line must post to an income account that is not
    one of `taxed`, and those lines must add up to the credit's net amount -- the rule
    credit_creation applies to a credit it proposes. Header tax alone misses an item line
    posting straight into a tax account (packet review 2026-10-16, F2).
    """
    net = Decimal(0)
    for line in lines:
        if line.get("taxline") == "T":
            continue  # the native tax line; header tax already carries it
        if line.get("foreignamount") is None:
            # A returned item's cost pair carries no transaction amount, so it cannot
            # move the order total or tax; the net check below still has to hold.
            if line.get("accttype") in INVENTORY_COST_TYPES:
                continue
            return "unproven"
        amount = _decimal(line.get("foreignamount"))
        if amount == 0:
            continue
        account = _id(line.get("expenseaccount"))
        if account is None or account in taxed or line.get("accttype") not in NET_ACCOUNT_TYPES:
            return "unproven"
        net += amount
    return "income" if net == tax - total else "unproven"  # header amounts are negative


def _summed(credits):
    return {
        "complete": True,
        "credits": credits,
        "total": str(sum((Decimal(c["total"]) for c in credits), Decimal(0))),
        "tax": str(sum((Decimal(c["tax"]) for c in credits), Decimal(0))),
    }


async def read_invoice_credits(
    request, candidates, nodes, owned, currency_id, *, billing=(), taxed=frozenset(), returns=(), reachable=frozenset()
):
    """Credit memos created from one of this order's invoices, with their amounts.

    `complete: False` means unknown, never zero. A credit on an invoice shared with
    another order cannot be attributed to this one.

    The same call reads the order's invoices: credits are subtracted from the sales order,
    so the comparison counts them only when the invoices equal it (packet review
    2026-10-16, F1). Every credit is reported with its `posting` (see `_posting`); the
    reader never drops one, and reconciliation counts the credits only when all are
    "income" and the order has no refund tax adjustment (rounds 2-4, F3-F6).

    The order's own return credits (C15) are reported apart, as `return_credits`: credits
    created from a return authorization the order reaches, whether applied to the invoice
    (a candidate here) or not (`returns`). The invoice credits are exactly what they were
    before C15; a problem with a return credit makes only `return_credits` unknown. Their
    rows share the read but have their own budget, so they never use up the invoice
    credits' rows; only a page longer than both budgets together is unknown for both.
    """
    extra = [identifier for identifier in returns if identifier not in candidates]
    if not candidates and not extra:
        return {**_summed([]), "return_credits": _summed([])}
    try:
        return await _read_credits(request, candidates, extra, nodes, owned, reachable, currency_id, billing, taxed)
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        # NetSuiteEvidenceError is a ValueError. The refund proof above stands; the
        # credits are unknown for this observation and the next scan reads them again.
        reason = str(exc) if isinstance(exc, ValueError) else "invoice_credit_unproven"
        if candidates:
            return {"complete": False, "reason": reason}
        # No invoice credit to read, exactly as before C15: the read was the return credits' alone.
        return {**_summed([]), "return_credits": {"complete": False, "reason": reason}}


async def _read_credits(request, candidates, extra, nodes, owned, reachable, currency_id, billing, taxed):
    if len(candidates) > MAX_INVOICE_CREDITS:
        raise ValueError("invoice_credit_budget")
    if not billing or any((nodes.get(i) or {}).get("type") != "CustInvc" for i in billing):
        raise ValueError("invoice_credit_billing_unproven")
    if any(identifier not in owned for identifier in billing):
        raise ValueError("invoice_credit_ownership_ambiguous")
    returned = None  # why the return credits are unknown, once that is known
    if len(candidates) + len(extra) > MAX_INVOICE_CREDITS:
        returned, extra = {"complete": False, "reason": "return_credit_budget"}, []
        if not candidates:
            return {**_summed([]), "return_credits": returned}
    ids = [*candidates, *extra, *billing]
    limit = MAX_INVOICE_CREDIT_ROWS + (MAX_RETURN_CREDIT_ROWS if extra else 0)
    result = await request(
        "POST",
        "/query/v1/suiteql",
        params={"limit": limit + 1},
        body={
            "q": "SELECT t.id, t.tranid, t.type, t.foreigntotal, t.taxtotal, t.currency, t.posting, "
            "t.voided, tl.mainline, tl.taxline, tl.createdfrom, tl.foreignamount, tl.expenseaccount, "
            "a.accttype FROM transaction t "
            "JOIN transactionline tl ON tl.transaction=t.id "
            "LEFT JOIN account a ON a.id=tl.expenseaccount "
            f"WHERE t.id IN ({','.join(ids)}) AND (t.type='CustCred' OR tl.mainline='T')"
        },
    )
    rows, complete = _collection(result)
    if not complete or len(rows) > limit:
        raise ValueError("invoice_credit_read_incomplete")
    returning = sum(1 for row in rows if _id(row.get("id")) in extra)
    if len(rows) - returning > MAX_INVOICE_CREDIT_ROWS:
        raise ValueError("invoice_credit_read_incomplete")
    if returning > MAX_RETURN_CREDIT_ROWS:
        returned = {"complete": False, "reason": "return_credit_read_incomplete"}
    headers, lines, broken = {}, defaultdict(list), set()
    for row in rows:
        identifier = _id(row.get("id"))
        if identifier not in ids:
            raise ValueError("invoice_credit_read_incomplete")
        if row.get("mainline") == "T" and identifier not in headers:
            headers[identifier] = row
        elif row.get("mainline") == "F":
            lines[identifier].append(row)
        else:
            broken.add(identifier)  # a duplicated header, or a line of no known kind
    # Every invoice credit and invoice has exactly one header; only credits have lines.
    complete_headers = all(i in headers and i not in broken for i in (*candidates, *billing))
    if not complete_headers or not set(lines) <= {*candidates, *extra}:
        raise ValueError("invoice_credit_read_incomplete")
    invoiced_total = invoiced_tax = Decimal(0)
    for identifier in billing:
        row = headers[identifier]
        total, tax = _decimal(row.get("foreigntotal")), _decimal(row.get("taxtotal"))
        if (
            row.get("type") != "CustInvc"
            or str(row.get("currency")) != str(currency_id)
            or row.get("posting") != "T"
            or row.get("voided") != "F"
            or total is None
            or tax is None
            or total < 0
            or tax < 0
        ):
            raise ValueError("invoice_credit_invoice_unproven")
        invoiced_total, invoiced_tax = invoiced_total + total, invoiced_tax + tax
    credits, applied = [], []
    for identifier in candidates:
        row = headers[identifier]
        origin = _id(row.get("createdfrom"))
        if (
            row.get("type") != "CustCred"
            or str(row.get("currency")) != str(currency_id)
            or row.get("posting") != "T"
            or row.get("voided") != "F"
        ):
            raise ValueError("invoice_credit_unproven")
        total, tax = _decimal(row.get("foreigntotal")), _decimal(row.get("taxtotal"))
        if total is None or tax is None or total >= 0 or tax > 0:
            raise ValueError("invoice_credit_amount_unproven")
        if origin is not None and origin not in nodes:
            # An origin the graph never saw is unresolved, not "not an invoice":
            # skipping it would certify a partial sum.
            raise ValueError("invoice_credit_origin_unproven")
        kind = (nodes.get(origin) or {}).get("type")
        if kind == "RtnAuth" and origin in reachable:
            applied.append(identifier)  # created from a return the order reaches: a return credit (C15)
            continue
        if kind != "CustInvc":
            continue  # another order's return credit, or standalone: applied, not an invoice credit
        if origin not in owned or identifier not in owned:
            raise ValueError("invoice_credit_ownership_ambiguous")
        credits.append(
            {
                "id": identifier,
                "number": row.get("tranid"),
                "origin_id": origin,
                "origin_type": "CustInvc",
                "invoice_id": origin,
                "total": str(Decimal(0) - total),
                "tax": str(Decimal(0) - tax),
                "posting": _posting(lines.get(identifier, ()), total, tax, taxed),
            }
        )
    if returned is None:
        returned = _return_credits(
            sorted({*applied, *extra}, key=int), headers, lines, broken, nodes, owned, reachable, currency_id, taxed
        )
    return {
        **_summed(credits),
        "invoices": {"ids": list(billing), "total": str(invoiced_total), "tax": str(invoiced_tax)},
        "return_credits": returned,
    }


def _return_credits(identifiers, headers, lines, broken, nodes, owned, reachable, currency_id, taxed):
    """The order's own return credits (C15), or why they are unknown; never raises.

    Each must be created from a return authorization the order reaches. One that another
    order shares -- its return authorization, or the credit itself applied to another
    order's invoice -- makes them ambiguous: it is the order's return, so it is never
    skipped. The reader classifies each credit's posting; reconciliation decides.
    """
    try:
        credits = []
        for identifier in identifiers:
            row = headers.get(identifier)
            if row is None or identifier in broken:
                raise ValueError("return_credit_read_incomplete")
            if (
                row.get("type") != "CustCred"
                or str(row.get("currency")) != str(currency_id)
                or row.get("posting") != "T"
                or row.get("voided") != "F"
            ):
                raise ValueError("return_credit_unproven")
            total, tax = _decimal(row.get("foreigntotal")), _decimal(row.get("taxtotal"))
            if total is None or tax is None or total >= 0 or tax > 0:
                raise ValueError("return_credit_amount_unproven")
            origin = _id(row.get("createdfrom"))
            if origin not in reachable or (nodes.get(origin) or {}).get("type") != "RtnAuth":
                raise ValueError("return_credit_origin_unproven")
            if origin not in owned or identifier not in owned:
                return {"complete": False, "reason": "return_credit_ownership_ambiguous"}
            credits.append(
                {
                    "id": identifier,
                    "number": row.get("tranid"),
                    "origin_id": origin,
                    "origin_type": "RtnAuth",
                    "total": str(Decimal(0) - total),
                    "tax": str(Decimal(0) - tax),
                    "posting": _posting(lines.get(identifier, ()), total, tax, taxed),
                }
            )
        return _summed(credits)
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        return {"complete": False, "reason": str(exc) if isinstance(exc, ValueError) else "return_credit_unproven"}


def order_deposits(nodes, reachable, owned, documents):
    """The order's customer deposits with their status and total, from the graph's own rows; None when
    any is unproven.

    Every non-voided deposit the graph reaches from the order counts. One shared with another order
    is not this order's alone, so the answer is unknown, never "no deposits" (PR #422 review round 1,
    F1). A deposit whose status or total the rows disagree on, or did not carry, is unknown too.
    """
    deposits = []
    for identifier in sorted(reachable, key=int):
        metadata = nodes.get(identifier) or {}
        if metadata.get("type") != "CustDep" or metadata.get("voided") == "T":
            continue
        status, total = documents.get(identifier) or (None, None)
        if identifier not in owned or not isinstance(status, str):
            return None
        try:
            amount = _positive(total)
        except (ValueError, TypeError, ArithmeticError):
            return None
        deposits.append({"id": identifier, "total": str(amount), "status": status})
    return deposits


def _ownership(edges, reachable, order_id):
    """Each document's graph parents, and the documents that resolve to this order alone.

    A shared invoice/deposit/credit cannot assign its whole refund to this order. Ownership
    requires every relevant upstream document to resolve to this root.
    """
    parents = defaultdict(set)
    for previous, following, before, after in edges:
        if before in _PARENTS.get(after, set()):
            parents[following].add(previous)
    owned = {order_id}
    for _ in range(MAX_DEPTH):
        owned.update(node for node in reachable if parents[node] and parents[node] <= owned)
    return parents, owned


def _attribute_tagged(tagged, nodes, reachable, owned, parents, allocations, documents):
    """Split the tagged credits the native graph did not reach into ``(accepted, rejected)``.

    A credit booked by hand is the order's only through the return authorization it names,
    which this order must own (C09, 2026-10-25). One that NetSuite also links to a document
    outside the order is shared, and fails the read as any shared credit does. It must have
    been created from nothing, from a document of this order, or from the very refund the
    graph shows paying it. While its authorization is still open, that authorization could
    pay the customer a second time, so its refund is left out; the hold reported in
    ``tagged_credits_rejected`` keeps the order from matching whatever the amounts
    (order_reconciliation.held_by_open_return_authorization, C09-R1).
    """
    accepted, rejected = [], []
    for credit in sorted(tagged, key=int):
        if credit in reachable:
            continue  # reached natively: the native rules count it, once
        authorization, origin = tagged[credit]["return_id"], tagged[credit]["created_from"]
        if authorization not in owned or (nodes.get(authorization) or {}).get("type") != "RtnAuth":
            reason = "return_authorization_not_in_order"
        elif not parents[credit] <= owned:
            raise ValueError("refund_allocation_ambiguous")
        elif origin is not None and origin not in owned and credit not in allocations.get(origin, ()):
            reason = "created_from_unproven"
        elif (documents.get(authorization) or (None, None))[0] not in SETTLED_RETURN_STATUSES:
            reason = "return_authorization_open"
        else:
            accepted.append(credit)
            continue
        rejected.append({"id": credit, "reason": reason, "return_authorization_id": authorization})
    return accepted, rejected


async def collect_refunds(
    reader,
    order_id,
    subsidiary_id,
    currency_id,
    *,
    order_reference,
    adjustment_profile=None,
    request_links_reader=None,
    graph_reader=None,
    tagged_reader=None,
):
    if any(_id(value) is None for value in (order_id, subsidiary_id, currency_id)):
        raise ValueError("invalid_refund_scope")
    calls = 0

    async def request(*args, **kwargs):
        nonlocal calls
        if calls >= MAX_REFUND_CALLS:
            raise ValueError("refund_read_budget")
        calls += 1
        return await reader.request(*args, **kwargs)

    nodes, request_links, recheck_links = await (request_links_reader or read_request_links)(
        request, order_id, subsidiary_id, currency_id, order_reference
    )
    tagged, rejected = await (tagged_reader or read_tagged_credits)(
        request, order_id, subsidiary_id, currency_id, order_reference
    )
    # A credit the order's refund requests link is theirs already; its tag adds nothing.
    tagged = {credit: value for credit, value in tagged.items() if credit not in nodes}
    # Custom ownership is additive. Standard upstream links are still traversed
    # and can veto a conflicting/shared credit; application proof stays native.
    # Tagged credits are read with the order, at no extra call, but join it only
    # once its graph shows they may (_attribute_tagged).
    frontier, visited, reachable = {order_id, *nodes, *tagged}, set(), {order_id, *nodes}
    edges = {(order_id, identifier, "SalesOrd", node["type"]) for identifier, node in nodes.items()}
    nodes.update({credit: dict(value["node"]) for credit, value in tagged.items()})
    allocations = defaultdict(set)
    cash_refunds = set()
    documents = {}
    for _ in range(MAX_DEPTH):
        if not frontier:
            break
        result = (
            await graph_reader(request, frontier)
            if graph_reader
            else await request(
                "POST", "/query/v1/suiteql", params={"limit": MAX_EDGES + 1}, body={"q": _query(frontier)}
            )
        )
        rows, complete = _collection(result)
        if not complete or len(rows) > MAX_EDGES:
            raise ValueError("refund_graph_incomplete")
        for row in rows:
            previous, following = _id(row.get("previousdoc")), _id(row.get("nextdoc"))
            if not previous or not following or not ({previous, following} & frontier):
                raise ValueError("refund_graph_identity_unproven")
            for prefix, identifier in (("previous", previous), ("next", following)):
                metadata = {
                    key: row.get(prefix + key) for key in ("type", "currency", "subsidiary", "posting", "voided")
                }
                if not isinstance(metadata["type"], str) or (identifier in nodes and nodes[identifier] != metadata):
                    raise ValueError("refund_graph_changed")
                nodes[identifier] = metadata
                # Kept apart from the node metadata the request links also describe; two rows that
                # disagree leave the document's status unknown rather than failing the refund proof.
                seen = (row.get(prefix + "status"), row.get(prefix + "total"))
                if documents.setdefault(identifier, seen) != seen:
                    documents[identifier] = (None, None)
            before, after = nodes[previous]["type"], nodes[following]["type"]
            edges.add((previous, following, before, after))
            if previous in frontier and before in _PARENTS.get(after, set()):
                reachable.add(following)
                if after == "CashRfnd":
                    cash_refunds.add(following)
            # NetSuite persists refunded deposits as refund -> deposit application.
            # The same Payment direction is verified for refund -> credit memo.
            if following in frontier and before == "CustRfnd" and after in {"DepAppl", "CustCred"}:
                allocations[previous].add(following)
            if previous in frontier and after == "CustRfnd" and before in {"DepAppl", "CustCred"}:
                allocations[following].add(previous)
        visited.update(frontier)
        frontier = reachable - visited
        if len(reachable) > MAX_DOCUMENTS or len(edges) > MAX_EDGES:
            raise ValueError("refund_graph_budget")
    if frontier:
        raise ValueError("refund_graph_depth")

    parents, owned = _ownership(edges, reachable, order_id)
    accepted, held = _attribute_tagged(tagged, nodes, reachable, owned, parents, allocations, documents)
    if accepted:
        # Owned through its return authorization, as if NetSuite linked the two.
        edges.update((tagged[credit]["return_id"], credit, "RtnAuth", "CustCred") for credit in accepted)
        reachable.update(accepted)
        if len(reachable) > MAX_DOCUMENTS or len(edges) > MAX_EDGES:
            raise ValueError("refund_graph_budget")
        parents, owned = _ownership(edges, reachable, order_id)
    # A refused credit stays an observed participant, but its refund is not this order's.
    refused = {entry["id"] for entry in held}
    for refund in list(allocations):
        allocations[refund] -= refused
        if not allocations[refund]:
            del allocations[refund]
    for identifier in owned:
        metadata = nodes.get(identifier)
        if metadata and (metadata["currency"] != currency_id or metadata["subsidiary"] != subsidiary_id):
            raise ValueError("refund_document_scope_mismatch")
    if any(not documents <= owned for documents in allocations.values()) or not cash_refunds <= owned:
        raise ValueError("refund_allocation_ambiguous")

    total, included, amounts = Decimal(0), [], {}
    # What this order's refunds paid out of each of its own credit memos. Only an allocation the
    # graph proved owned counts, so a refund that also pays another order's credit never makes
    # that credit this order's (packet review 2026-10-19, F2).
    credit_refunds = defaultdict(Decimal)
    application_documents = set()
    for identifier in sorted(set(allocations) | cash_refunds, key=int):
        metadata = nodes[identifier]
        if metadata["voided"] == "T":
            continue
        if metadata["voided"] != "F" or metadata["posting"] != "T":
            raise ValueError("refund_posting_unproven")
        if metadata["currency"] != currency_id or metadata["subsidiary"] != subsidiary_id:
            raise ValueError("refund_scope_mismatch")
        kind = _REFUND_TYPES[metadata["type"]]
        record = await request("GET", f"/record/v1/{kind}/{identifier}", params={"expandSubResources": "true"})
        if (
            _id(record.get("id")) != identifier
            or (record.get("currency") or {}).get("id") != currency_id
            or (record.get("subsidiary") or {}).get("id") != subsidiary_id
        ):
            raise ValueError("refund_record_scope_mismatch")
        amount = _positive(record.get("total"))
        if kind == "customerrefund":
            problems = []
            items = _sublist(record, "apply", "refund_apply", frozenset({"apply", "doc", "line", "amount"}), problems)
            if problems or items is None:
                raise ValueError("refund_application_incomplete")
            seen, applied, this_order, matched_docs = set(), Decimal(0), Decimal(0), set()
            for item in items:
                if item.get("apply") is False:
                    continue
                doc, line = (item.get("doc") or {}).get("id"), _id(item.get("line"))
                if item.get("apply") is not True or not _id(doc) or line is None or (doc, line) in seen:
                    raise ValueError("refund_application_ambiguous")
                seen.add((doc, line))
                application_documents.add(doc)
                value = _positive(item.get("amount"))
                applied += value
                if doc in allocations[identifier]:
                    this_order += value
                    matched_docs.add(doc)
                    if nodes[doc]["type"] == "CustCred":
                        credit_refunds[doc] += value
            if applied != amount or matched_docs != allocations[identifier]:
                raise ValueError("refund_application_changed")
            amount = this_order
        total += amount
        included.append(identifier)
        amounts[identifier] = amount
    verify_request_allocations(request_links, allocations, amounts)
    adjustments = (
        await read_tax_adjustments(
            request,
            request_links,
            adjustment_profile,
            order_id,
            subsidiary_id,
            currency_id,
            order_reference,
            allocations,
        )
        if adjustment_profile
        else []
    )
    taxed = frozenset()
    if adjustment_profile:
        profile = RefundAdjustmentProfile.model_validate(adjustment_profile)
        taxed = frozenset(profile.tax_accounts) | frozenset(profile.tax_item_accounts.values())
    invoice_credits = await read_invoice_credits(
        request,
        invoice_credit_candidates({k: v for k, v in nodes.items() if k not in refused}, parents),
        nodes,
        owned,
        currency_id,
        billing=billing_documents(nodes, reachable),
        taxed=taxed,
        # A hand-booked credit attached through its RA (C09) is a refund, already counted from the
        # refund that pays it, and its origin is that refund, not the RA: never a return credit (C15).
        returns=return_credit_candidates(
            {k: v for k, v in nodes.items() if k not in refused and k not in set(accepted)}, parents, reachable
        ),
        reachable=reachable,
    )
    await recheck_links(accepted)
    request_ids = sorted((link["request_id"] for link in request_links), key=int)
    transaction_ids = [order_id, *sorted(({*nodes, *application_documents} - {order_id}), key=int)]
    dependency_limit = MAX_DEPENDENCIES - len(request_ids)
    return {
        "amount": total,
        "refund_count": len(included),
        "record_ids": included,
        "request_links": request_links,
        "tax_adjustments": adjustments,
        "invoice_credits": invoice_credits,
        "credit_refunds": {doc: str(value) for doc, value in sorted(credit_refunds.items(), key=lambda x: int(x[0]))},
        # Credits booked by hand and tagged with the order (C09): those its graph owns through their
        # return authorization, and every other one with why it was left out of the amount.
        "tagged_credits": accepted,
        "tagged_credits_rejected": sorted(
            (entry for entry in [*rejected, *held] if entry["id"] not in reachable), key=lambda e: int(e["id"])
        ),
        # The order's invoices and cash sales and its deposits, from the graph's own rows: what
        # reconciliation needs to settle a canceled order (decided 2026-10-22).
        "billing_ids": billing_documents(nodes, reachable),
        "deposits": order_deposits(nodes, reachable, owned, documents),
        # Retain every observed participant, including voided refunds and
        # parents outside the owned set. They can invalidate the proof later.
        # This inventories this read's inputs; it is not a change-feed watermark.
        "dependency_manifest": {
            "version": 1,
            "order_id": order_id,
            "transaction_ids": transaction_ids[:dependency_limit],
            "refund_requests": request_ids,
            "truncated": len(transaction_ids) > dependency_limit,
        },
    }
