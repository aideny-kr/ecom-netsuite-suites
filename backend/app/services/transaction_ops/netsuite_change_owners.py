"""Resolve changed native identities to candidate sales orders, without money proof.

Old/removed ownership comes from the persisted dependency index. This reader
finds current native/custom ownership, including newly attached documents. It
never chooses one owner from an ambiguous group or assigns refund amounts.
"""

import asyncio
from datetime import datetime, timezone

from app.services.transaction_ops.netsuite_changes import _FIELD, _REFERENCE
from app.services.transaction_ops.netsuite_reader import (
    NetSuiteEvidenceError,
    _account,
    _collection,
    _id,
    authenticated_reader,
)
from app.services.transaction_ops.netsuite_refunds import _PARENTS

MAX_OWNER_CALLS = 9  # Initial records, six ancestor levels, custom links, roots.
MAX_DOCUMENTS = 100
MAX_ROWS = 200
MAX_DEPTH = 6


def _ids(values, limit=MAX_DOCUMENTS):
    if not isinstance(values, (list, tuple, set)) or len(values) > limit:
        raise NetSuiteEvidenceError("dependency_owner_scope_invalid")
    result = set()
    for value in values:
        identifier = _id(value)
        if identifier is None or int(identifier) <= 0:
            raise NetSuiteEvidenceError("dependency_owner_scope_invalid")
        result.add(str(int(identifier)))
    return result


def _sql_ids(values):
    return ",".join(sorted(values, key=int))


def _owner_query(frontier):
    """Read only traversable ancestor edges, before applying the provider cap.

    The refund evidence query intentionally reads both directions and financial
    context. Reusing it here expands inventory adjustments into thousands of
    unrelated shipments and multiplies journals by their subsidiary lines.
    Candidate ownership needs only the native identities and supported types.
    """
    ids = _sql_ids(frontier)
    pairs = [
        f"(n.type='{child}' AND p.type IN ({','.join(repr(parent) for parent in sorted(parents))}))"
        for child, parents in sorted(_PARENTS.items())
    ]
    pairs.append("(n.type='CustRfnd' AND p.type IN ('DepAppl','CustCred'))")
    projection = (
        "SELECT l.previousdoc,l.nextdoc,p.type AS previoustype,n.type AS nexttype "
        "FROM NextTransactionLink l JOIN transaction p ON p.id=l.previousdoc "
        "JOIN transaction n ON n.id=l.nextdoc "
    )
    # Keep the two indexed link directions separate. Their OR caused the
    # verified REST role to time out on ordinary twenty-document pages. UNION
    # deduplicates the final edge set before the existing completeness cap.
    return (
        "SELECT DISTINCT edges.previousdoc,edges.nextdoc,edges.previoustype,edges.nexttype FROM ("
        + projection
        + f"WHERE l.nextdoc IN ({ids}) AND ({' OR '.join(pairs)}) "
        "UNION "
        + projection
        + f"WHERE l.previousdoc IN ({ids}) AND p.type='CustRfnd' AND n.type IN ('DepAppl','CustCred') "
        ") edges ORDER BY edges.previousdoc,edges.nextdoc"
    )


async def collect_order_candidates(
    request, subsidiary_id, reference_field, document_ids, order_ids, references, *, bulk=False
):
    # Bulk mode stays below SuiteQL IN/REST response limits; incomplete graphs
    # are split by the durable scan, never accepted as a negative result.
    document_limit, row_limit = (1000, 999) if bulk else (MAX_DOCUMENTS, MAX_ROWS)
    documents, roots = _ids(document_ids, document_limit), _ids(order_ids, document_limit)
    inventory = []
    if (
        not _id(subsidiary_id)
        or int(subsidiary_id) <= 0
        or not isinstance(reference_field, str)
        or not _FIELD.fullmatch(reference_field)
        or not isinstance(references, (list, tuple, set))
        or len(references) > document_limit
        or any(not isinstance(value, str) or not _REFERENCE.fullmatch(value) for value in references)
        or len(documents | roots) > document_limit
    ):
        raise NetSuiteEvidenceError("dependency_owner_scope_invalid")
    references = set(references)

    async def query(sql):
        rows, complete = _collection(
            await request(
                "POST",
                "/query/v1/suiteql",
                params={"limit": row_limit + 1, "offset": 0},
                body={"q": sql},
            )
        )
        if not complete or len(rows) > row_limit:
            raise NetSuiteEvidenceError("dependency_owner_page_incomplete")
        if bulk:
            inventory.append(rows)
        return rows

    async def records(identifiers, refs):
        conditions = []
        if identifiers:
            conditions.append(f"t.id IN ({_sql_ids(identifiers)})")
        if refs:
            quoted = ",".join("'" + ref + "'" for ref in sorted(refs))
            conditions.append(f"(t.type='SalesOrd' AND t.{reference_field} IN ({quoted}))")
        if not conditions:
            return []
        # Combining native IDs and references with OR was slow on the verified
        # REST role even for tiny results. Union the identity sets before the join;
        # the outer query still checks every matching header/subsidiary and
        # applies its completeness limit only to the final deduplicated rows.
        condition = conditions[0]
        if len(conditions) > 1:
            identities = " UNION ".join(f"SELECT t.id FROM transaction t WHERE {part}" for part in conditions)
            condition = f"t.id IN ({identities})"
        rows = await query(
            f"SELECT DISTINCT t.id,t.type,t.{reference_field} AS order_reference,m.subsidiary "
            "FROM transaction t LEFT JOIN transactionline m "
            "ON m.transaction=t.id AND m.mainline='T' AND t.type='SalesOrd' "
            f"WHERE {condition} ORDER BY t.id"
        )
        seen = set()
        for row in rows:
            identifier = _id(row.get("id"))
            if (
                not identifier
                or int(identifier) <= 0
                or identifier in seen
                or not isinstance(row.get("type"), str)
                or (row.get("type") == "SalesOrd" and not _id(row.get("subsidiary")))
                or (
                    identifier not in identifiers
                    and not (row["type"] == "SalesOrd" and row.get("order_reference") in refs)
                )
            ):
                raise NetSuiteEvidenceError("dependency_owner_identity_unproven")
            seen.add(identifier)
        return rows

    candidates, outside = set(), set()

    def accept_roots(rows):
        remaining = set()
        for row in rows:
            identifier = str(row["id"])
            if row["type"] != "SalesOrd":
                remaining.add(identifier)
            elif str(row["subsidiary"]) != str(subsidiary_id):
                outside.add(identifier)
            elif isinstance(row.get("order_reference"), str) and _REFERENCE.fullmatch(row["order_reference"]):
                candidates.add(row["order_reference"])
        return remaining

    initial = await records(documents | roots, references)
    frontier = accept_roots(initial)
    remaining = documents - {str(row["id"]) for row in initial if row["type"] == "SalesOrd"}
    visited, native_roots = set(), set()
    for _ in range(MAX_DEPTH):
        if not frontier:
            break
        rows = await query(_owner_query(frontier))
        parents = set()
        for row in rows:
            previous, following = _id(row.get("previousdoc")), _id(row.get("nextdoc"))
            before, after = row.get("previoustype"), row.get("nexttype")
            if (
                not previous
                or not following
                or not isinstance(before, str)
                or not isinstance(after, str)
                or (previous not in frontier and following not in frontier)
            ):
                raise NetSuiteEvidenceError("dependency_owner_identity_unproven")
            if following in frontier and before in _PARENTS.get(after, set()):
                if before == "SalesOrd":
                    native_roots.add(previous)
                else:
                    parents.add(previous)
            # Native customer refunds also appear on the reverse side of the
            # application edge. Follow both supported orientations to parents.
            if previous in frontier and before == "CustRfnd" and after in {"DepAppl", "CustCred"}:
                parents.add(following)
            if following in frontier and after == "CustRfnd" and before in {"DepAppl", "CustCred"}:
                parents.add(previous)
        visited.update(frontier)
        frontier = parents - visited
        if len(visited | frontier | native_roots) > document_limit:
            raise NetSuiteEvidenceError("dependency_owner_budget")
    if frontier:
        raise NetSuiteEvidenceError("dependency_owner_depth")

    # Custom refund links can supply the only ownership path for standalone
    # credit memos/refunds; keep all candidates rather than choosing an owner.
    custom_roots, custom_refs = set(), set()
    remaining.update(visited)
    if remaining:
        ids = _sql_ids(remaining)
        rows = await query(
            "SELECT r.id,r.custrecord_refreq_so_link AS order_id,"
            "r.custrecord_refreq_order_number AS order_reference "
            "FROM customrecord_fw_refund_requests r "
            f"WHERE r.custrecord_refreq_cm_link IN ({ids}) OR r.custrecord_refreq_refund_link IN ({ids}) "
            f"OR r.custrecord_refreq_cust_dep_link IN ({ids}) ORDER BY r.id"
        )
        for row in rows:
            if not _id(row.get("id")):
                raise NetSuiteEvidenceError("dependency_owner_identity_unproven")
            if row.get("order_id") is not None:
                custom_roots.update(_ids([row["order_id"]], document_limit))
            ref = row.get("order_reference")
            if isinstance(ref, str) and _REFERENCE.fullmatch(ref):
                custom_refs.add(ref)
    if len(native_roots | custom_roots) > document_limit or len(custom_refs) > document_limit:
        raise NetSuiteEvidenceError("dependency_owner_budget")
    accept_roots(await records(native_roots | custom_roots, custom_refs))
    result = {"order_references": sorted(candidates), "outside_subsidiary_ids": sorted(outside, key=int)}
    if bulk:
        result["inventory"] = inventory  # Native rows retained for replay/audit; no monetary fields.
    return result


async def read_order_candidates(
    db,
    tenant_id,
    connection_id,
    account_id,
    subsidiary_id,
    reference_field,
    *,
    document_ids=(),
    order_ids=(),
    references=(),
    client=None,
    bulk=False,
):
    account = _account(account_id)
    try:
        async with asyncio.timeout(90 if bulk else 170):
            async with authenticated_reader(
                db,
                tenant_id,
                connection_id,
                account,
                client=client,
                max_api_calls=MAX_OWNER_CALLS,
            ) as reader:
                candidates = await collect_order_candidates(
                    reader.request,
                    subsidiary_id,
                    reference_field,
                    document_ids,
                    order_ids,
                    references,
                    bulk=bulk,
                )
    except (TimeoutError, NetSuiteEvidenceError) as exc:
        if bulk and (isinstance(exc, TimeoutError) or str(exc) == "read_timeout"):
            raise NetSuiteEvidenceError("dependency_owner_batch_timeout") from None
        raise
    return {
        **candidates,
        "provider": "netsuite",
        "evidence_use": "candidate_invalidation_only",
        "scope": {"connection_id": str(connection_id), "account_id": account, "subsidiary_id": str(subsidiary_id)},
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
