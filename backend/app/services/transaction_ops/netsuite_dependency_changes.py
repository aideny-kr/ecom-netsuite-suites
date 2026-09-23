"""Bounded native change inventories for candidate invalidation, never money proof.

Each stream keeps a fixed UTC window and a stable identity cursor. Completion
means this role's query was exhausted, not that the account exposes every change
or that any cached financial evidence is fresh. Deletions nominate both known
record namespaces because numeric IDs alone do not identify a NetSuite table.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from app.services.transaction_ops.netsuite_changes import _FIELD, _window
from app.services.transaction_ops.netsuite_reader import (
    NetSuiteEvidenceError,
    _account,
    _has_next,
    _id,
    authenticated_reader,
)

STREAMS = ("transactions", "transaction_lines", "transaction_links", "refund_requests", "deletions")
_TYPES = "'SalesOrd','CustInvc','CashSale','CustDep','DepAppl','RtnAuth','CustCred','CustRfnd','CashRfnd'"
_REQUEST_TABLE = "customrecord_fw_refund_requests"
_UTC_FORMAT = 'YYYY-MM-DD"T"HH24:MI:SS.FF6"Z"'


def _utc(column):
    return f"SYS_EXTRACT_UTC({column})"


def _stamp(expression):
    return f"TO_CHAR({expression},'{_UTC_FORMAT}') AS modified_utc"


def _dates(column, lower, upper):
    # The bare-column candidate bound avoids the full transaction-table scan
    # observed on the Framework REST role; exact UTC predicates decide inclusion.
    return (
        f"{column}>=TO_DATE('{lower[:19]}','YYYY-MM-DD HH24:MI:SS')-2 "
        f"AND {column}<TO_DATE('{upper[:19]}','YYYY-MM-DD HH24:MI:SS')+2 "
        f"AND {_utc(column)}>=TO_TIMESTAMP('{lower}','YYYY-MM-DD HH24:MI:SS.FF6') "
        f"AND {_utc(column)}<TO_TIMESTAMP('{upper}','YYYY-MM-DD HH24:MI:SS.FF6')"
    )


def change_query(stream, subsidiary_id, reference_field, start, end, after):
    lower, upper = _window(start, end)
    width = 2 if stream == "transaction_links" else 1
    if (
        stream not in STREAMS
        or not _id(subsidiary_id)
        or int(subsidiary_id) <= 0
        or not isinstance(reference_field, str)
        or not _FIELD.fullmatch(reference_field)
        or not isinstance(after, (tuple, list))
        or len(after) != width
        or any(type(value) is not int or not 0 <= value < 10**30 for value in after)
    ):
        raise NetSuiteEvidenceError("invalid_dependency_change_scope")
    subsidiary = int(subsidiary_id)
    if stream == "transactions":
        return (
            f"SELECT DISTINCT t.id,t.type,t.{reference_field} AS order_reference,m.subsidiary,"
            f"{_stamp(_utc('t.lastmodifieddate'))} "
            "FROM transaction t JOIN transactionline m ON m.transaction=t.id AND m.mainline='T' "
            f"WHERE m.subsidiary={subsidiary} AND t.type IN ({_TYPES}) AND t.id>{after[0]} "
            f"AND {_dates('t.lastmodifieddate', lower, upper)} ORDER BY t.id"
        )
    if stream == "transaction_lines":
        return (
            f"SELECT t.id,t.type,t.{reference_field} AS order_reference,m.subsidiary,"
            f"{_stamp('MAX(' + _utc('l.linelastmodifieddate') + ')')} "
            "FROM transactionline l JOIN transaction t ON t.id=l.transaction "
            "JOIN transactionline m ON m.transaction=t.id AND m.mainline='T' "
            f"WHERE m.subsidiary={subsidiary} AND t.type IN ({_TYPES}) AND t.id>{after[0]} "
            f"AND {_dates('l.linelastmodifieddate', lower, upper)} "
            f"GROUP BY t.id,t.type,t.{reference_field},m.subsidiary ORDER BY t.id"
        )
    if stream == "transaction_links":
        return (
            f"SELECT l.previousdoc,l.nextdoc,{_stamp('MAX(' + _utc('l.lastmodifieddate') + ')')} "
            "FROM NextTransactionLineLink l "
            f"WHERE (l.previousdoc>{after[0]} OR (l.previousdoc={after[0]} AND l.nextdoc>{after[1]})) "
            f"AND {_dates('l.lastmodifieddate', lower, upper)} "
            "AND EXISTS (SELECT 1 FROM transactionline m "
            f"WHERE m.mainline='T' AND m.subsidiary={subsidiary} "
            "AND (m.transaction=l.previousdoc OR m.transaction=l.nextdoc)) "
            "GROUP BY l.previousdoc,l.nextdoc ORDER BY l.previousdoc,l.nextdoc"
        )
    if stream == "refund_requests":
        # Includes requests before they have an SO link. Their reference/owner
        # must be resolved to the requested entity before scheduling an order.
        return (
            "SELECT r.id,r.custrecord_refreq_so_link AS order_id,"
            "r.custrecord_refreq_order_number AS order_reference,"
            f"{_stamp(_utc('r.lastmodified'))} FROM {_REQUEST_TABLE} r "
            f"WHERE r.id>{after[0]} AND {_dates('r.lastmodified', lower, upper)} ORDER BY r.id"
        )
    # deleteddate is an unzoned DATE on the verified REST role and rejects
    # SYS_EXTRACT_UTC. Use an intentionally over-inclusive +/-2-day envelope;
    # never append a false Z or use this value as an exact UTC watermark.
    # Collapsing duplicate identities is safe for candidate invalidation.
    return (
        "SELECT d.recordid AS id,TO_CHAR(MAX(d.deleteddate),'YYYY-MM-DD\"T\"HH24:MI:SS') AS del_date "
        f"FROM deletedrecord d WHERE d.recordid>{after[0]} "
        f"AND d.deleteddate>=TO_DATE('{lower[:19]}','YYYY-MM-DD HH24:MI:SS')-2 "
        f"AND d.deleteddate<TO_DATE('{upper[:19]}','YYYY-MM-DD HH24:MI:SS')+2 "
        "GROUP BY d.recordid ORDER BY d.recordid"
    )


def _items(body, page_size):
    try:
        rows, count, more, total = (body[k] for k in ("items", "count", "hasMore", "totalResults"))
        if (
            not isinstance(rows, list)
            or type(count) is not int
            or count != len(rows)
            or count > page_size + 1
            or type(total) is not int
            or total < count
            or type(more) is not bool
            or body.get("offset", 0) != 0
            or (more and count != page_size + 1)
            or (not more and (total != count or _has_next(body)))
        ):
            raise ValueError()
        return rows
    except (KeyError, ValueError, TypeError):
        raise NetSuiteEvidenceError("dependency_change_page_incomplete") from None


async def read_change_page(
    db,
    tenant_id,
    connection_id,
    account_id,
    subsidiary_id,
    reference_field,
    stream,
    start,
    end,
    *,
    after=None,
    page_size=20,
    client=None,
):
    account = _account(account_id)
    after = after if after is not None else ([0, 0] if stream == "transaction_links" else [0])
    query = change_query(stream, subsidiary_id, reference_field, start, end, after)
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise NetSuiteEvidenceError("invalid_dependency_change_scope")
    async with asyncio.timeout(80):
        async with authenticated_reader(
            db,
            tenant_id,
            connection_id,
            account,
            client=client,
            max_api_calls=1,
        ) as reader:
            body = await reader.request(
                "POST",
                "/query/v1/suiteql",
                params={"limit": page_size + 1, "offset": 0},
                body={"q": query},
            )
    changes, previous = [], tuple(after)
    try:
        for row in _items(body, page_size):
            raw = [row["previousdoc"], row["nextdoc"]] if stream == "transaction_links" else [row["id"]]
            if any(_id(value) is None or int(value) <= 0 for value in raw):
                raise ValueError()
            identity = tuple(int(value) for value in raw)
            if identity <= previous:
                raise ValueError()
            if stream == "deletions":
                raw_date = datetime.fromisoformat(row["del_date"])
                lower = start.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0) - timedelta(days=2)
                upper = end.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0) + timedelta(days=2)
                if raw_date.utcoffset() is not None or not lower <= raw_date < upper:
                    raise ValueError()
                modified = None
            else:
                modified = datetime.fromisoformat(row["modified_utc"])
                if modified.utcoffset() is None or not start <= modified < end:
                    raise ValueError()
            keys = [("transaction", str(value)) for value in identity]
            if stream in {"transactions", "transaction_lines"}:
                if row["type"] not in _TYPES.replace("'", "").split(",") or str(row["subsidiary"]) != str(
                    subsidiary_id
                ):
                    raise ValueError()
            if stream == "refund_requests":
                keys = [(_REQUEST_TABLE, str(identity[0]))]
                if row.get("order_id") is not None and not _id(row["order_id"]):
                    raise ValueError()
            if stream == "deletions":
                keys.append((_REQUEST_TABLE, str(identity[0])))
            changes.append(
                {
                    "cursor": list(identity),
                    "record_keys": keys,
                    "modified_at": modified.isoformat() if modified else None,
                    "deleted_at_raw": row["del_date"] if stream == "deletions" else None,
                    "transaction_type": row.get("type"),
                    "order_id": str(row["order_id"]) if row.get("order_id") is not None else None,
                    "order_reference": row.get("order_reference"),
                }
            )
            previous = identity
    except (KeyError, ValueError, TypeError, AttributeError):
        raise NetSuiteEvidenceError("dependency_change_page_incomplete") from None
    more = len(changes) > page_size
    return {
        "provider": "netsuite",
        "stream": stream,
        "scope": {
            "connection_id": str(connection_id),
            "account_id": account,
            "subsidiary_id": str(subsidiary_id),
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
        },
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "changes": changes[:page_size],
        "page_complete": True,
        "scan_complete": not more,
        "next_cursor": changes[page_size - 1]["cursor"] if more else None,
        "window_semantics": "conservative_date_envelope" if stream == "deletions" else "exact_utc",
        "evidence_use": "candidate_invalidation_only",
    }
