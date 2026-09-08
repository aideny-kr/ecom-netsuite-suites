"""Bounded native refund allocation graph; credits are never counted as returned money."""

import asyncio
import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from app.schemas.transaction_ops import _decimal
from app.services.transaction_ops.netsuite_reader import _account, _collection, _id, _sublist, authenticated_reader
from app.services.transaction_ops.netsuite_refund_requests import read_request_links, verify_request_allocations
from app.services.transaction_ops.refund_adjustments import RefundAdjustmentProfile, read_tax_adjustments

MAX_REFUND_CALLS = 24
MAX_DOCUMENTS = 100
MAX_EDGES = 200
MAX_DEPTH = 6
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
    async with asyncio.timeout(160):
        async with authenticated_reader(
            db, tenant_id, connection_id, account, client=client, max_api_calls=MAX_REFUND_CALLS
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


def _query(frontier):
    identifiers = ",".join(sorted(frontier, key=int))
    return (
        "SELECT DISTINCT l.previousdoc, l.nextdoc, p.type AS previoustype, n.type AS nexttype, "
        "p.currency AS previouscurrency, n.currency AS nextcurrency, "
        "pl.subsidiary AS previoussubsidiary, nl.subsidiary AS nextsubsidiary, "
        "p.posting AS previousposting, n.posting AS nextposting, "
        "p.voided AS previousvoided, n.voided AS nextvoided "
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


async def collect_refunds(reader, order_id, subsidiary_id, currency_id, *, order_reference, adjustment_profile=None):
    if any(_id(value) is None for value in (order_id, subsidiary_id, currency_id)):
        raise ValueError("invalid_refund_scope")
    calls = 0

    async def request(*args, **kwargs):
        nonlocal calls
        if calls >= MAX_REFUND_CALLS:
            raise ValueError("refund_read_budget")
        calls += 1
        return await reader.request(*args, **kwargs)

    nodes, request_links, recheck_links = await read_request_links(
        request, order_id, subsidiary_id, currency_id, order_reference
    )
    # Custom ownership is additive. Standard upstream links are still traversed
    # and can veto a conflicting/shared credit; application proof stays native.
    frontier, visited, reachable = {order_id, *nodes}, set(), {order_id, *nodes}
    edges = {(order_id, identifier, "SalesOrd", node["type"]) for identifier, node in nodes.items()}
    allocations = defaultdict(set)
    cash_refunds = set()
    for _ in range(MAX_DEPTH):
        if not frontier:
            break
        result = await request(
            "POST", "/query/v1/suiteql", params={"limit": MAX_EDGES + 1}, body={"q": _query(frontier)}
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

    # A shared invoice/deposit/credit cannot assign its whole refund to this order.
    # Ownership requires every relevant upstream document to resolve to this root.
    parents = defaultdict(set)
    for previous, following, before, after in edges:
        if before in _PARENTS.get(after, set()):
            parents[following].add(previous)
    owned = {order_id}
    for _ in range(MAX_DEPTH):
        owned.update(node for node in reachable if parents[node] and parents[node] <= owned)
    for identifier in owned:
        metadata = nodes.get(identifier)
        if metadata and (metadata["currency"] != currency_id or metadata["subsidiary"] != subsidiary_id):
            raise ValueError("refund_document_scope_mismatch")
    if any(not documents <= owned for documents in allocations.values()) or not cash_refunds <= owned:
        raise ValueError("refund_allocation_ambiguous")

    total, included, amounts = Decimal(0), [], {}
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
                value = _positive(item.get("amount"))
                applied += value
                if doc in allocations[identifier]:
                    this_order += value
                    matched_docs.add(doc)
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
    await recheck_links()
    return {
        "amount": total,
        "refund_count": len(included),
        "record_ids": included,
        "request_links": request_links,
        "tax_adjustments": adjustments,
    }
