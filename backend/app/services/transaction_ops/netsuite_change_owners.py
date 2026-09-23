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


def _ids(values):
    if not isinstance(values, (list, tuple, set)) or len(values) > MAX_DOCUMENTS:
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
    return (
        "SELECT DISTINCT l.previousdoc,l.nextdoc,p.type AS previoustype,n.type AS nexttype "
        "FROM NextTransactionLink l JOIN transaction p ON p.id=l.previousdoc "
        "JOIN transaction n ON n.id=l.nextdoc "
        f"WHERE (l.nextdoc IN ({ids}) AND ({' OR '.join(pairs)})) "
        f"OR (l.previousdoc IN ({ids}) AND p.type='CustRfnd' AND n.type IN ('DepAppl','CustCred')) "
        "ORDER BY l.previousdoc,l.nextdoc"
    )


async def collect_order_candidates(request, subsidiary_id, reference_field, document_ids, order_ids, references):
    documents, roots = _ids(document_ids), _ids(order_ids)
    if (
        not _id(subsidiary_id)
        or int(subsidiary_id) <= 0
        or not isinstance(reference_field, str)
        or not _FIELD.fullmatch(reference_field)
        or not isinstance(references, (list, tuple, set))
        or len(references) > MAX_DOCUMENTS
        or any(not isinstance(value, str) or not _REFERENCE.fullmatch(value) for value in references)
        or len(documents | roots) > MAX_DOCUMENTS
    ):
        raise NetSuiteEvidenceError("dependency_owner_scope_invalid")
    references = set(references)

    async def query(sql):
        rows, complete = _collection(
            await request(
                "POST",
                "/query/v1/suiteql",
                params={"limit": MAX_ROWS + 1, "offset": 0},
                body={"q": sql},
            )
        )
        if not complete or len(rows) > MAX_ROWS:
            raise NetSuiteEvidenceError("dependency_owner_page_incomplete")
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
        if len(visited | frontier | native_roots) > MAX_DOCUMENTS:
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
                custom_roots.update(_ids([row["order_id"]]))
            ref = row.get("order_reference")
            if isinstance(ref, str) and _REFERENCE.fullmatch(ref):
                custom_refs.add(ref)
    if len(native_roots | custom_roots) > MAX_DOCUMENTS or len(custom_refs) > MAX_DOCUMENTS:
        raise NetSuiteEvidenceError("dependency_owner_budget")
    accept_roots(await records(native_roots | custom_roots, custom_refs))
    return {"order_references": sorted(candidates), "outside_subsidiary_ids": sorted(outside, key=int)}


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
):
    account = _account(account_id)
    async with asyncio.timeout(170):
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
            )
    return {
        **candidates,
        "provider": "netsuite",
        "evidence_use": "candidate_invalidation_only",
        "scope": {"connection_id": str(connection_id), "account_id": account, "subsidiary_id": str(subsidiary_id)},
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
