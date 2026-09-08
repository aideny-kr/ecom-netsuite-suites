"""Fixed native change-window reads; references nominate fresh two-system reads."""

import asyncio
import re
from datetime import datetime, timedelta, timezone

from app.services.transaction_ops.netsuite_reader import (
    NetSuiteEvidenceError,
    _account,
    _has_next,
    _id,
    authenticated_reader,
)

_REFERENCE = re.compile(r"R[0-9]{9}(?:-[A-Z0-9]+)?\Z")
_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")


def _window(start, end):
    if (
        not isinstance(start, datetime)
        or not isinstance(end, datetime)
        or start.utcoffset() is None
        or end.utcoffset() is None
        or not timedelta(0) < end - start <= timedelta(days=32)
    ):
        raise NetSuiteEvidenceError("invalid_change_window")
    return [d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f") for d in (start, end)]


async def read_changed_orders(
    db,
    tenant_id,
    connection_id,
    account_id,
    subsidiary_id,
    reference_field,
    start,
    end,
    *,
    after_id=0,
    page_size=20,
    client=None,
):
    lower, upper = _window(start, end)
    account = _account(account_id)
    if (
        not _id(subsidiary_id)
        or int(subsidiary_id) <= 0
        or not isinstance(reference_field, str)
        or not _FIELD.fullmatch(reference_field)
        or type(after_id) is not int
        or not 0 <= after_id < 10**30
        or type(page_size) is not int
        or not 1 <= page_size <= 100
    ):
        raise NetSuiteEvidenceError("invalid_change_scope")
    modified = "SYS_EXTRACT_UTC(t.lastmodifieddate)"
    query = (
        f"SELECT t.id,t.{reference_field} AS order_reference,t.type,l.subsidiary,"
        f'TO_CHAR({modified},\'YYYY-MM-DD"T"HH24:MI:SS.FF6"Z"\') AS modified_utc '
        "FROM transaction t JOIN transactionline l ON l.transaction=t.id AND l.mainline='T' "
        f"WHERE t.type='SalesOrd' AND l.subsidiary={int(subsidiary_id)} AND t.id>{after_id} "
        f"AND REGEXP_INSTR(t.{reference_field},'^R[0-9]{{9}}(-[A-Z0-9]+)?$')=1 "
        f"AND {modified}>=TO_TIMESTAMP('{lower}','YYYY-MM-DD HH24:MI:SS.FF6') "
        f"AND {modified}<TO_TIMESTAMP('{upper}','YYYY-MM-DD HH24:MI:SS.FF6') ORDER BY t.id"
    )
    async with asyncio.timeout(80):
        async with authenticated_reader(
            db, tenant_id, connection_id, account, client=client, max_api_calls=1
        ) as transport:
            body = await transport.request(
                "POST", "/query/v1/suiteql", params={"limit": page_size + 1, "offset": 0}, body={"q": query}
            )
    try:
        rows = body["items"]
        count = body["count"]
        more = body["hasMore"]
        total = body["totalResults"]
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
        cursor = after_id
        orders = []
        for row in rows:
            identifier = _id(row["id"])
            reference = row["order_reference"]
            modified_at = datetime.fromisoformat(row["modified_utc"])
            if (
                identifier is None
                or int(identifier) <= cursor
                or row["type"] != "SalesOrd"
                or str(row["subsidiary"]) != str(subsidiary_id)
                or not isinstance(reference, str)
                or not _REFERENCE.fullmatch(reference)
                or modified_at.utcoffset() is None
                or not start <= modified_at < end
            ):
                raise ValueError()
            cursor = int(identifier)
            orders.append({"id": cursor, "number": reference, "updated_at": modified_at.isoformat()})
    except (KeyError, ValueError, TypeError, AttributeError):
        raise NetSuiteEvidenceError("destination_change_page_incomplete") from None
    sentinel = len(orders) > page_size
    return {
        "provider": "netsuite",
        "scope": {
            "connection_id": str(connection_id),
            "account_id": account,
            "subsidiary_id": str(subsidiary_id),
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
        },
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "orders": orders[:page_size],
        "page_complete": True,
        "scan_complete": not sentinel,
        "next_after_id": orders[page_size - 1]["id"] if sentinel else None,
    }
