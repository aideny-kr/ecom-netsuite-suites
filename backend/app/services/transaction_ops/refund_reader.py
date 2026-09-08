"""Completed Solidus refunds through the tenant's existing Celigo PostgreSQL link.

The saved export identifies the authorized connection only. Its SQL, filters,
hooks, and delta state never execute. Reimbursements are included, because
excluding them would silently omit refunds issued through the return workflow.
"""

import asyncio
import json
import re
from datetime import datetime, timezone

import httpx

from app.schemas.transaction_ops import _decimal
from app.services.transaction_ops import source_reader as source

_REFERENCE = re.compile(r"R[0-9]{9}(?:-[A-Z0-9]+)?\Z")


def _refund_query(reference):
    if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
        raise source.SourceReadError("invalid_order_reference", 422)
    return (
        "SELECT o.number AS order_reference, o.currency, COUNT(r.id)::text AS refund_count, "
        "COUNT(r.id) FILTER (WHERE NULLIF(r.transaction_id, '') IS NULL)::text AS pending_count, "
        "COALESCE(SUM(r.amount) FILTER (WHERE NULLIF(r.transaction_id, '') IS NOT NULL), 0)::text AS amount, "
        "CASE WHEN COUNT(r.id)<=100 THEN COALESCE(JSONB_AGG(JSONB_BUILD_OBJECT("
        "'id',r.id::text,'payment_number',p.number,'amount',r.amount::text) ORDER BY r.id) "
        "FILTER (WHERE r.id IS NOT NULL AND NULLIF(r.transaction_id, '') IS NOT NULL), '[]'::jsonb) END AS events "
        "FROM spree_orders o LEFT JOIN spree_payments p ON p.order_id = o.id "
        "LEFT JOIN spree_refunds r ON r.payment_id = p.id "
        f"WHERE o.number = '{reference}' GROUP BY o.id, o.number, o.currency ORDER BY o.id LIMIT 2"
    )


def _count(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[0-9]{1,10}", str(value)):
        raise source.SourceReadError("invalid_refund_evidence")
    return int(value)


async def _read_rows(db, tenant_id, step_id, query, limit, *, client=None):
    step, connection, token, region = await source._load_source(db, tenant_id, step_id)
    if step.adaptor_type != "RDBMSExport":
        raise source.SourceReadError("unsupported_refund_source", 422)
    owned = client is None
    http = client or httpx.AsyncClient(timeout=source._HTTP_TIMEOUT, follow_redirects=False)
    transport = source._Transport(http, token, region)
    try:
        async with asyncio.timeout(40):
            live = await transport.request("GET", f"/v1/connections/{step.connection_celigo_id}")
            if (
                live.get("_id") != step.connection_celigo_id
                or live.get("type") != "rdbms"
                or not isinstance(live.get("rdbms"), dict)
                or live["rdbms"].get("type") != "postgresql"
            ):
                raise source.SourceReadError("unsupported_refund_source", 422)
            result = await transport.request(
                "POST",
                "/v1/exports/preview",
                body={
                    "name": "Order refund reconciliation read",
                    "_connectionId": step.connection_celigo_id,
                    "type": "test",
                    "test": {"limit": limit},
                    "rdbms": {"query": query},
                },
            )
            source._check_envelope(result)
            stages = result.get("stages", [])
            if not isinstance(stages, list):
                raise source.SourceReadError("invalid_refund_evidence")
            for stage in stages:
                source._check_envelope(stage, nullable_errors=True)
            data = result.get("data")
            if not isinstance(data, list) or len(data) > limit or any(not isinstance(row, dict) for row in data):
                raise source.SourceReadError("invalid_refund_evidence")
            for row in data:
                source._check_envelope(row)
            return data, step, connection
    except (httpx.HTTPError, TimeoutError):
        raise source.SourceReadError("refund_source_unavailable") from None
    finally:
        if owned:
            await http.aclose()


def _events(value, count, amount, pending):
    try:
        if isinstance(value, str):
            value = json.loads(value)
        if pending or not isinstance(value, list) or len(value) > 100 or len(value) != count:
            return [], False
        seen, total, events = set(), 0, []
        for row in value:
            identifier, payment = row["id"], row["payment_number"]
            number = _decimal(row["amount"])
            if (
                not isinstance(identifier, str)
                or not re.fullmatch(r"[0-9]{1,30}", identifier)
                or identifier in seen
                or not isinstance(payment, str)
                or not re.fullmatch(r"[A-Z0-9]{1,100}", payment)
                or number is None
                or number <= 0
            ):
                return [], False
            seen.add(identifier)
            total += number
            events.append({"id": identifier, "payment_number": payment, "amount": str(number)})
        return (events, True) if total == amount else ([], False)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return [], False


async def read_solidus_refunds(db, tenant_id, step_id, order_reference, *, client=None):
    data, step, connection = await _read_rows(db, tenant_id, step_id, _refund_query(order_reference), 2, client=client)
    if len(data) != 1:
        raise source.SourceReadError("refund_order_identity_unproven")
    row = data[0]
    if row.get("order_reference") != order_reference or not re.fullmatch(r"[A-Z]{3}", str(row.get("currency"))):
        raise source.SourceReadError("refund_order_identity_unproven")
    count, pending = _count(row.get("refund_count")), _count(row.get("pending_count"))
    try:
        amount = _decimal(row.get("amount"))
    except ValueError:
        raise source.SourceReadError("invalid_refund_evidence") from None
    if pending > count or amount < 0 or (count == 0 and amount != 0):
        raise source.SourceReadError("invalid_refund_evidence")
    events, events_complete = _events(row.get("events"), count, amount, pending)
    return {
        "events": events,
        "events_complete": events_complete,
        "source": "solidus_postgresql",
        "order_reference": order_reference,
        "currency": row["currency"],
        "complete": pending == 0,
        "amount": str(amount) if pending == 0 else None,
        "refund_count": count,
        "unconfirmed_count": pending,
        "source_step_id": str(step.id),
        "connection_id": str(connection.id),
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


async def read_refund_order_page(db, tenant_id, step_id, since, until, *, after_id=0, client=None):
    if (
        not isinstance(since, datetime)
        or not isinstance(until, datetime)
        or since.utcoffset() is None
        or until.utcoffset() is None
        or not 0 < (until - since).total_seconds() <= 31 * 86400
        or type(after_id) is not int
        or not 0 <= after_id < 10**30
    ):
        raise source.SourceReadError("invalid_refund_window", 422)
    lower, upper = since.astimezone(timezone.utc).isoformat(), until.astimezone(timezone.utc).isoformat()
    query = (
        "SELECT o.id::text AS id, o.number, (MAX(r.updated_at) AT TIME ZONE 'UTC')::text AS changed_at "
        "FROM spree_refunds r JOIN spree_payments p ON p.id=r.payment_id "
        "JOIN spree_orders o ON o.id=p.order_id "
        f"WHERE r.updated_at >= ('{lower}'::timestamptz AT TIME ZONE 'UTC') "
        f"AND r.updated_at <= ('{upper}'::timestamptz AT TIME ZONE 'UTC') "
        f"AND o.id > {after_id} GROUP BY o.id,o.number ORDER BY o.id LIMIT 101"
    )
    rows, _, _ = await _read_rows(db, tenant_id, step_id, query, 101, client=client)
    previous, references = after_id, set()
    for row in rows:
        identifier = str(row.get("id", ""))
        if (
            not re.fullmatch(r"[0-9]{1,30}", identifier)
            or int(identifier) <= previous
            or not isinstance(row.get("number"), str)
            or not _REFERENCE.fullmatch(row["number"])
            or row["number"] in references
        ):
            raise source.SourceReadError("refund_page_identity_unproven")
        try:
            changed = datetime.fromisoformat(row["changed_at"].replace("Z", "+00:00"))
            if changed.utcoffset() is None or not since <= changed <= until:
                raise ValueError
        except (ValueError, TypeError, AttributeError, KeyError):
            raise source.SourceReadError("refund_page_window_unproven") from None
        previous = int(identifier)
        references.add(row["number"])
    selected = rows[:100]
    return {
        "page_complete": True,
        "orders": [{"id": str(row["id"]), "number": row["number"]} for row in selected],
        "next_after_id": int(selected[-1]["id"]) if len(rows) > 100 else None,
    }
