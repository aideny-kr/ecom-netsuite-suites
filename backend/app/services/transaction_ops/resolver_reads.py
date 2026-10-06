"""Live NetSuite reads for the resolver: the document chain, bounded SuiteQL, and schema.

Spec 2026-10-01 (accounting resolver) §5, block B6. Each function takes an authenticated
reader (`netsuite_reader.authenticated_reader`), so the connection, budget, timeout and
throttling rules stay in one place. None of them writes. Nothing exposes them to the chat
agent yet (B9).

- `chain_read(record)`: the documents around one transaction. It walks up the created-from
  links to the top (usually the sales order), then down two levels: the order's deposits,
  fulfillments, invoices and returns, then what was created from those (credit memos,
  deposit applications). It uses only the form known to work on this connector, a
  `transactionline.createdfrom` join on the main line, bounded by an explicit id list.
  An open createdfrom join fails here, and the link tables return 500. It reads at most
  `MAX_CHAIN_DOCUMENTS` documents, and the starting document and its ancestors are always
  among them. When the depth limit stops the walk, one bounded probe checks the next
  level. `complete` is true only when nothing is known to be unread: no page, cap or
  hop limit cut the chain, and the probe found nothing below. `unread_above` names a
  parent above the hop limit; `unread_below_depth` says deeper documents exist.
- `netsuite_query(sql)`: SuiteQL as written, with a row bound. SuiteQL cannot write: the
  engine enforces that, so there is no text check to get wrong.
- `netsuite_schema(record_type)`: a record type's fields, and each sublist's line fields,
  from the REST metadata catalog, on demand. The live catalog carries no requiredness, so `requirements_known` is false;
  required fields come from the curated registry when a write is validated.

Ids and record-type names are checked as values before they reach a query or a path.
"""

from __future__ import annotations

import re

from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError, _collection

MAX_CHAIN_DOCUMENTS = 40
MAX_HOPS_UP = 3
MAX_DEPTH_DOWN = 2
DEFAULT_QUERY_ROWS = 50
MAX_QUERY_ROWS = 1000
SUITEQL = "/query/v1/suiteql"

_ID = re.compile(r"^[1-9][0-9]{0,11}$")
_RECORD_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SELECT = (
    "SELECT t.id, t.type, t.tranid, BUILTIN.DF(t.status) AS status, t.foreigntotal, t.taxtotal, "
    "t.trandate, t.memo, tl.createdfrom "
    "FROM transaction t JOIN transactionline tl ON tl.transaction = t.id AND tl.mainline = 'T'"
)
TYPES = {
    "SalesOrd": "sales order",
    "CustInvc": "invoice",
    "CashSale": "cash sale",
    "CustCred": "credit memo",
    "CustDep": "customer deposit",
    "DepAppl": "deposit application",
    "CustPymt": "customer payment",
    "CustRfnd": "customer refund",
    "CashRfnd": "cash refund",
    "RtnAuth": "return authorization",
    "ItemShip": "item fulfillment",
    "ItemRcpt": "item receipt",
    "Journal": "journal entry",
}


def _internal_id(value) -> str:
    text = str(value).strip() if isinstance(value, str | int) and not isinstance(value, bool) else ""
    if not _ID.match(text):
        raise ValueError(f"not a NetSuite internal id: {value!r}")
    return text


def _id(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("id")
    try:
        return _internal_id(value)
    except ValueError:
        return None


def _document(row: dict, depth: int) -> dict:
    return {
        "id": _id(row.get("id")),
        "type": TYPES.get(row.get("type"), row.get("type")),
        "number": row.get("tranid"),
        "status": row.get("status"),
        "total": row.get("foreigntotal"),
        "tax": row.get("taxtotal"),
        "date": row.get("trandate"),
        "memo": row.get("memo"),
        "created_from": _id(row.get("createdfrom")),
        "depth": depth,
    }


async def _rows(reader, column: str, ids: list[str]) -> tuple[list[dict], bool]:
    sql = f"{_SELECT} WHERE {column} IN ({','.join(ids)}) ORDER BY t.id"
    body = await reader.request("POST", SUITEQL, params={"limit": MAX_CHAIN_DOCUMENTS, "offset": 0}, body={"q": sql})
    return _collection(body)


async def chain_read(reader, record_id) -> dict:
    root = _internal_id(record_id)
    try:
        rows, complete = await _rows(reader, "t.id", [root])
        if not rows:
            return {"root": root, "error": "record_not_found"}
        path = [rows[0]]  # root first, then each parent
        walked = {root}
        while len(path) <= MAX_HOPS_UP:
            parent = _id(path[-1].get("createdfrom"))
            if parent is None or parent in walked:
                break
            rows, ok = await _rows(reader, "t.id", [parent])
            complete = complete and ok
            if not rows:
                complete = False  # a parent we could not read
                break
            walked.add(parent)
            path.append(rows[0])
        top = path[-1]
        above = _id(top.get("createdfrom"))
        unread_above = above if above is not None and above not in walked else None
        if unread_above is not None:
            complete = False  # the hop limit (or an unreadable parent) stopped the walk below the real top
        # The starting document and its ancestors go in first, so the cap can never drop them.
        documents = {_id(row.get("id")): (row, distance) for distance, row in enumerate(reversed(path))}
        frontier, depth = [_id(top.get("id"))], 0
        while frontier and depth < MAX_DEPTH_DOWN:
            depth += 1
            rows, ok = await _rows(reader, "tl.createdfrom", frontier)
            complete = complete and ok
            frontier = []
            for row in rows:
                identifier = _id(row.get("id"))
                if identifier is None:
                    continue
                if identifier in documents:
                    # An ancestor met at its own level is already listed, but its children still count.
                    if documents[identifier][1] == depth and identifier not in frontier:
                        frontier.append(identifier)
                    continue
                if len(documents) >= MAX_CHAIN_DOCUMENTS:
                    complete = False
                    break
                documents[identifier] = (row, depth)
                frontier.append(identifier)
        unread_below = None
        if frontier:
            # The depth limit stopped the walk. One bounded probe says whether anything is below,
            # so `complete` is a checked claim, not an assumption.
            below, ok = await _rows(reader, "tl.createdfrom", frontier) if complete else ([], False)
            if not ok or any(_id(r.get("id")) not in documents for r in below):
                complete, unread_below = False, MAX_DEPTH_DOWN
    except NetSuiteEvidenceError as exc:
        return {"root": root, "error": str(exc)}
    ordered = sorted(documents.values(), key=lambda pair: (pair[1], int(_id(pair[0].get("id")) or 0)))
    return {
        "root": root,
        "top": _id(top.get("id")),
        "documents": [_document(row, depth) for row, depth in ordered],
        "complete": complete,
        "unread_above": unread_above,
        "unread_below_depth": unread_below,
    }


async def netsuite_query(reader, sql, *, limit: int = DEFAULT_QUERY_ROWS) -> dict:
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("a query is required")
    if type(limit) is not int or not 1 <= limit <= MAX_QUERY_ROWS:
        raise ValueError(f"limit must be 1..{MAX_QUERY_ROWS}")
    try:
        body = await reader.request("POST", SUITEQL, params={"limit": limit, "offset": 0}, body={"q": sql})
        rows, complete = _collection(body)
    except NetSuiteEvidenceError as exc:
        return {"error": str(exc)}
    return {"rows": rows, "row_count": len(rows), "complete": complete}


async def netsuite_schema(reader, record_type) -> dict:
    if not isinstance(record_type, str) or not _RECORD_TYPE.match(record_type):
        raise ValueError(f"not a record type name: {record_type!r}")
    from app.services.chat.record_metadata_service import _parse_properties_shape

    try:
        raw = await reader.request("GET", f"/record/v1/metadata-catalog/{record_type}")
    except NetSuiteEvidenceError as exc:
        return {"record_type": record_type, "error": str(exc)}
    metadata = _parse_properties_shape({"metadata": raw}, record_type)
    if metadata is None:
        return {"record_type": record_type, "error": "schema_unavailable"}

    def spec(field):
        return {"name": field.name, "label": field.label, "type": field.type}

    return {
        "record_type": record_type,
        "fields": [spec(f) for f in metadata.fields],
        "sublists": _sublists(raw),
        "requirements_known": metadata.requirements_known,
    }


def _sublists(raw) -> dict[str, list[dict]]:
    """Line fields per sublist, read from the catalog's JSON schema (`<name>.items[].<field>`).

    The shared metadata parser keeps only top-level fields, so lines are read here.
    """
    out = {}
    properties = raw.get("properties") if isinstance(raw, dict) else None
    for name, prop in (properties or {}).items():
        items = ((prop or {}).get("properties") or {}).get("items") if isinstance(prop, dict) else None
        fields = ((items or {}).get("items") or {}).get("properties") if isinstance(items, dict) else None
        if isinstance(items, dict) and items.get("type") == "array" and isinstance(fields, dict):
            out[name] = [
                {"name": key, "label": (value or {}).get("title") or key, "type": (value or {}).get("type")}
                for key, value in fields.items()
            ]
    return out
