"""Bounded native batches; preserve the existing record and refund proof readers.

SuiteQL batches identity and graph reads. Record API projections remain the source
of commercial amounts: SuiteQL custom totals are not interchangeable with native
tax/shipping totals. A staged result retains its actual observation time.
"""

import asyncio
import copy
import re
from datetime import datetime, timezone

from app.services.transaction_ops.call_meter import observed_calls
from app.services.transaction_ops.netsuite_reader import (
    NetSuiteEvidenceError,
    _account,
    _collection,
    _id,
    authenticated_reader,
)
from app.services.transaction_ops.netsuite_refund_requests import read_request_links, request_query
from app.services.transaction_ops.netsuite_refunds import _PARENTS, MAX_DEPTH, _query, collect_refunds

MAX_ORDERS = 10
MAX_CALLS = 32
MAX_CONCURRENT_CALLS = 7  # Per authenticated bulk collection, not the account's other integrations.
MAX_ROWS = 1000
_REFERENCE = re.compile(r"R[0-9]{9}(?:-[A-Z0-9]+)?\Z")
_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")


async def _collect_concurrent(worker, values, *, parallel=True):
    """At most ten bounded branches; the shared reader caps actual wire calls.

    Drain every child before propagating failure/cancellation so the caller's
    reservation cannot settle while an orphan is still spending it. Preserve
    the original provider exception for the existing retry/fallback classifier.
    """
    if not parallel:
        return [await worker(value) for value in values]
    tasks = [asyncio.create_task(worker(value)) for value in values]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def collection(rows):
    return {"items": copy.deepcopy(rows), "count": len(rows), "totalResults": len(rows), "hasMore": False}


async def query(reader, sql, limit=MAX_ROWS):
    body = await reader.request("POST", "/query/v1/suiteql", params={"limit": limit, "offset": 0}, body={"q": sql})
    rows, complete = _collection(body)
    if not complete or len(rows) > limit or body.get("offset", 0) != 0:
        raise NetSuiteEvidenceError("bulk_page_incomplete")
    return rows


def references(values):
    if (
        not isinstance(values, list)
        or not 1 <= len(values) <= MAX_ORDERS
        or any(not isinstance(v, str) or len(v) > 255 or not _REFERENCE.fullmatch(v) for v in values)
        or len(set(values)) != len(values)
    ):
        raise NetSuiteEvidenceError("invalid_bulk_references")
    return values


async def collect_orders(reader, refs, reference_field, subsidiary_id):
    """One all-date/all-subsidiary identity query; ambiguity is never filtered out."""
    references(refs)
    if not _id(subsidiary_id) or not isinstance(reference_field, str) or not _FIELD.fullmatch(reference_field):
        raise NetSuiteEvidenceError("invalid_bulk_scope")
    literals = ",".join(f"'{ref}'" for ref in refs)
    rows = await query(
        reader,
        (
            f"SELECT t.id,t.tranid,t.type,t.{reference_field} AS order_reference "
            f"FROM transaction t WHERE t.type='SalesOrd' AND t.{reference_field} IN ({literals}) ORDER BY t.id"
        ),
        2 * MAX_ORDERS + 1,
    )
    grouped = {ref: [] for ref in refs}
    seen = set()
    for row in rows:
        identifier = _id(row.get("id"))
        ref = row.get("order_reference")
        if not identifier or identifier in seen or ref not in grouped or row.get("type") != "SalesOrd":
            raise NetSuiteEvidenceError("invalid_bulk_identity")
        seen.add(identifier)
        grouped[ref].append(row)

    async def expand(item):
        ref, matches = item
        # Preserve the single-reader's two-match cap and incomplete lookup marker.
        raw = collection(matches[:2])
        if len(matches) > 2:
            raw.update(totalResults=len(matches), hasMore=True)
        try:
            with observed_calls() as calls:
                result = await reader.read_matches(
                    raw, order_reference=ref, reference_field=reference_field, subsidiary_id=subsidiary_id
                )
        except BaseException:
            # Set synchronously before a released slot can wake another sender.
            reader.aborted = True
            raise
        result["api_calls"] = calls.calls
        return ref, result

    return dict(await _collect_concurrent(expand, grouped.items()))


class RefundGraphBatch:
    """A complete union of native neighborhoods, partitioned by the old verifier.

    This cache is collection-local. It never treats an unqueried node as empty;
    custom reverse-ownership checks remain fresh native queries.
    """

    def __init__(self, reader, requests, edges, scanned):
        self.reader, self.requests, self.edges, self.scanned = reader, requests, edges, scanned

    @classmethod
    async def collect(cls, reader, orders):
        roots = {order["record_id"] for order in orders.values()}
        refs = references(list(orders))
        if any(not _id(root) for root in roots):
            raise NetSuiteEvidenceError("invalid_bulk_root")
        ids = ",".join(sorted(roots, key=int))
        literals = ",".join(f"'{ref}'" for ref in refs)
        requests = await query(
            reader,
            request_query(
                f"r.custrecord_refreq_so_link IN ({ids}) OR r.custrecord_refreq_order_number IN ({literals})"
            ),
        )
        if any(str(row.get("order_id")) not in roots and row.get("order_reference") not in refs for row in requests):
            raise NetSuiteEvidenceError("invalid_bulk_request_scope")
        frontier = roots | {
            _id(row.get(key)) for row in requests for key in ("credit_id", "deposit_id") if _id(row.get(key))
        }
        scanned, edges = set(), []
        # Follow the same native relationship types as collect_refunds. A larger
        # union cannot erase shared ownership; every touching edge is retained.
        for _ in range(MAX_DEPTH):
            if not frontier:
                break
            if len(frontier) > MAX_ROWS:
                raise NetSuiteEvidenceError("bulk_graph_budget")
            rows = await query(reader, _query(frontier))
            for row in rows:
                p, n = _id(row.get("previousdoc")), _id(row.get("nextdoc"))
                if not p or not n or not {p, n} & frontier:
                    raise NetSuiteEvidenceError("invalid_bulk_edge")
                if row not in edges:
                    edges.append(row)
            scanned.update(frontier)
            frontier = {
                row["nextdoc"]
                for row in rows
                if row["previousdoc"] in frontier
                and row.get("previoustype") in _PARENTS.get(row.get("nexttype"), set())
            } - scanned
            if len(edges) > MAX_ROWS or len(scanned) > MAX_ROWS:
                raise NetSuiteEvidenceError("bulk_graph_budget")
        return cls(reader, requests, edges, scanned)

    def parallel_fits(self, roots, adjustment_profile):
        """Admit only cached native graphs whose worst-case GETs fit the budget.

        Custom links/adjustments or incomplete graphs stay serial, so a scarce
        budget finishes earlier proofs instead of stranding every branch. This
        overcounts touching refunds; it schedules work, never proves ownership.
        """
        if self.requests or adjustment_profile:
            return False
        needed = 0
        for root in roots:
            reachable = {root}
            for _ in range(MAX_DEPTH):
                reachable |= {
                    row["nextdoc"]
                    for row in self.edges
                    if row["previousdoc"] in reachable
                    and row.get("previoustype") in _PARENTS.get(row.get("nexttype"), set())
                }
            if not reachable <= self.scanned:
                return False
            refunds = {
                row[prefix + "doc"]
                for row in self.edges
                if {row["previousdoc"], row["nextdoc"]} & reachable
                for prefix in ("previous", "next")
                if row.get(prefix + "type") in {"CashRfnd", "CustRfnd"}
            }
            needed += len(refunds)
        return needed <= self.reader.max_api_calls - self.reader.calls

    async def links(self, request, order_id, subsidiary_id, currency_id, reference):
        initial = request_query(
            f"r.custrecord_refreq_so_link={order_id} OR r.custrecord_refreq_order_number='{reference}'"
        )
        used = False

        async def partition(method, path, **kwargs):
            nonlocal used
            if not used and method == "POST" and path == "/query/v1/suiteql" and kwargs.get("body") == {"q": initial}:
                used = True
                return collection(
                    [
                        row
                        for row in self.requests
                        if str(row.get("order_id")) == order_id or row.get("order_reference") == reference
                    ]
                )
            return await request(method, path, **kwargs)

        return await read_request_links(partition, order_id, subsidiary_id, currency_id, reference)

    async def graph(self, request, frontier):
        if frontier <= self.scanned:
            return collection([row for row in self.edges if {row["previousdoc"], row["nextdoc"]} & frontier])
        return await request("POST", "/query/v1/suiteql", params={"limit": 201}, body={"q": _query(frontier)})


async def read_orders(db, tenant_id, connection_id, account_id, subsidiary_id, refs, reference_field):
    account = _account(account_id)
    try:
        async with asyncio.timeout(90):
            async with authenticated_reader(
                db,
                tenant_id,
                connection_id,
                account,
                max_api_calls=MAX_CALLS,
                max_concurrent_calls=MAX_CONCURRENT_CALLS,
            ) as reader:
                results = await collect_orders(reader, refs, reference_field, subsidiary_id)
                for value in results.values():
                    value["scope"] = {
                        "connection_id": str(connection_id),
                        "account_id": account,
                        "subsidiary_id": subsidiary_id,
                        "reference_field": reference_field,
                    }
                return {
                    "orders": results,
                    "api_calls": reader.calls,
                    "credential_fingerprint": reader.credential_fingerprint,
                    "concurrency_peak": reader.peak_concurrency,
                }
    except TimeoutError:
        raise NetSuiteEvidenceError("bulk_read_timeout") from None


async def read_refunds(db, tenant_id, connection_id, account_id, subsidiary_id, targets, *, adjustment_profile=None):
    # Use precisely the same target checks as the single reader before batching.
    from app.services.transaction_ops.netsuite_refunds import refund_scope

    account = _account(account_id)
    scopes = {
        ref: refund_scope(connection_id, account, subsidiary_id, ref, target, adjustment_profile)
        for ref, target in targets.items()
    }
    try:
        async with asyncio.timeout(90):
            async with authenticated_reader(
                db,
                tenant_id,
                connection_id,
                account,
                max_api_calls=MAX_CALLS,
                max_concurrent_calls=MAX_CONCURRENT_CALLS,
            ) as reader:
                batch = await RefundGraphBatch.collect(reader, {ref: scope[0] for ref, scope in scopes.items()})

                async def expand(item):
                    ref, (order, currency_id, currency) = item
                    try:
                        with observed_calls() as calls:
                            result = await collect_refunds(
                                reader,
                                order["record_id"],
                                subsidiary_id,
                                currency_id,
                                order_reference=ref,
                                adjustment_profile=adjustment_profile,
                                request_links_reader=batch.links,
                                graph_reader=batch.graph,
                            )
                        return ref, {
                            **result,
                            "amount": str(result["amount"]),
                            "complete": True,
                            "provider": "netsuite",
                            "account_id": account,
                            "subsidiary_id": subsidiary_id,
                            "connection_id": str(connection_id),
                            "order_reference": ref,
                            "currency": currency,
                            "api_calls": calls.calls,
                            "observed_at": datetime.now(timezone.utc).isoformat(),
                        }
                    except (ValueError, NetSuiteEvidenceError):
                        # Keep sibling successes. The runner explicitly falls back for
                        # failed orders, with a new reservation; no negative caching.
                        return ref, None

                results = dict(
                    await _collect_concurrent(
                        expand,
                        scopes.items(),
                        parallel=batch.parallel_fits(
                            [scope[0]["record_id"] for scope in scopes.values()], adjustment_profile
                        ),
                    )
                )
                return {
                    "refunds": {ref: value for ref, value in results.items() if value is not None},
                    "api_calls": reader.calls,
                    "credential_fingerprint": reader.credential_fingerprint,
                    "concurrency_peak": reader.peak_concurrency,
                }
    except TimeoutError:
        raise NetSuiteEvidenceError("bulk_read_timeout") from None
