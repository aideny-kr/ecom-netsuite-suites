"""Completed Solidus refunds through the tenant's existing Celigo PostgreSQL link.

The saved export identifies the authorized connection only. Its SQL, filters,
hooks, and delta state never execute. Reimbursements are included, because
excluding them would silently omit refunds issued through the return workflow.
"""

import asyncio
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
        "COALESCE(SUM(r.amount) FILTER (WHERE NULLIF(r.transaction_id, '') IS NOT NULL), 0)::text AS amount "
        "FROM spree_orders o LEFT JOIN spree_payments p ON p.order_id = o.id "
        "LEFT JOIN spree_refunds r ON r.payment_id = p.id "
        f"WHERE o.number = '{reference}' GROUP BY o.id, o.number, o.currency ORDER BY o.id LIMIT 2"
    )


def _count(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[0-9]{1,10}", str(value)):
        raise source.SourceReadError("invalid_refund_evidence")
    return int(value)


async def read_solidus_refunds(db, tenant_id, step_id, order_reference, *, client=None):
    query = _refund_query(order_reference)
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
                    "test": {"limit": 2},
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
            if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
                raise source.SourceReadError("refund_order_identity_unproven")
            row = data[0]
            source._check_envelope(row)
            if row.get("order_reference") != order_reference or not re.fullmatch(r"[A-Z]{3}", str(row.get("currency"))):
                raise source.SourceReadError("refund_order_identity_unproven")
            count, pending = _count(row.get("refund_count")), _count(row.get("pending_count"))
            try:
                amount = _decimal(row.get("amount"))
            except ValueError:
                raise source.SourceReadError("invalid_refund_evidence") from None
            if pending > count or amount < 0 or (count == 0 and amount != 0):
                raise source.SourceReadError("invalid_refund_evidence")
            return {
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
    except (httpx.HTTPError, TimeoutError):
        raise source.SourceReadError("refund_source_unavailable") from None
    finally:
        if owned:
            await http.aclose()
