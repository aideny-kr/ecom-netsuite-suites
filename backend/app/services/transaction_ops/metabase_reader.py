"""Fixed Solidus replica reads through a tenant-owned Metabase MCP connection.

No SQL, caller-selected tables/columns or saved questions. A sentinel row drives
keyset pagination; Metabase's missing continuation token does not prove coverage.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from app.models.mcp_connector import McpConnector
from app.services.mcp_client_service import call_external_mcp_tool
from app.services.metabase_oauth_service import is_metabase
from app.services.public_http import validate_endpoint

ORDER_FIELDS = (
    "id",
    "number",
    "total",
    "additional_tax_total",
    "included_tax_total",
    "currency",
    "business_entity",
    "business_entity_slug",
    "completed_at",
    "updated_at",
)
REFUND_FIELDS = (
    "id",
    "payment_id",
    "amount",
    "transaction_id",
    "created_at",
    "updated_at",
    "reimbursement_id",
    "state",
)
PAYMENT_FIELDS = ("id", "order_id", "state", "created_at", "updated_at")
TABLES = {
    "orders": ("spree_orders", ORDER_FIELDS),
    "refunds": ("spree_refunds", REFUND_FIELDS),
    "payments": ("spree_payments", PAYMENT_FIELDS),
}
_REFERENCE = re.compile(r"R[0-9]{9}(?:-[A-Z0-9]+)?\Z")


class ReplicaReadError(ValueError):
    def __init__(self, code="replica_evidence_incomplete"):
        self.code = code
        super().__init__(code)


class ReplicaBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    connector_id: UUID
    server_url: str
    database_id: int = Field(gt=0, strict=True)
    database_name: str = Field(min_length=1, max_length=200)
    schema_name: str = Field(min_length=1, max_length=100)
    orders_table_id: int = Field(gt=0, strict=True)
    refunds_table_id: int = Field(gt=0, strict=True)
    payments_table_id: int = Field(gt=0, strict=True)
    # This contract must be verified at binding time against schema + Solidus API.
    # Metabase may label a naive PostgreSQL timestamp with its report timezone
    # without changing the stored wall value. Do not convert that label to UTC.
    timestamp_storage: Literal["utc_naive"]

    @field_validator("server_url")
    @classmethod
    def valid_endpoint(cls, value):
        value = validate_endpoint(value)
        if urlsplit(value).path != "/api/metabase-mcp":
            raise ValueError("Expected a Metabase MCP endpoint")
        return value


def _binding(value):
    try:
        return ReplicaBinding.model_validate(value)
    except (ValueError, TypeError):
        raise ReplicaReadError("invalid_replica_binding") from None


def _id(value):
    if type(value) is int and 0 < value < 2**63:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value) and int(value) < 2**63:
        return int(value)
    raise ReplicaReadError("invalid_replica_identity")


def _money(value):
    if type(value) not in (str, int, Decimal):
        raise ReplicaReadError("inexact_replica_amount")
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -6 or amount.adjusted() > 30:
            raise ValueError()
        return format(amount, "f")
    except (ValueError, ArithmeticError):
        raise ReplicaReadError("invalid_replica_amount") from None


def _source_time(value):
    """Preserve verified UTC-naive storage wall time, ignoring the display offset."""
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc).isoformat()
    except (ValueError, TypeError):
        raise ReplicaReadError("invalid_replica_timestamp") from None


def _window(start, end):
    if (
        not isinstance(start, datetime)
        or not isinstance(end, datetime)
        or start.utcoffset() is None
        or end.utcoffset() is None
        or not timedelta(0) < end - start <= timedelta(days=32)
    ):
        raise ReplicaReadError("invalid_replica_window")
    return [value.astimezone(timezone.utc).replace(tzinfo=None).isoformat() for value in (start, end)]


def _field(binding, table, name):
    return ["field", {}, [binding.database_name, binding.schema_name, TABLES[table][0], name]]


async def _connector(db, tenant_id, binding):
    connector = await db.scalar(
        select(McpConnector).where(
            McpConnector.id == binding.connector_id,
            McpConnector.tenant_id == tenant_id,
            McpConnector.status == "active",
            McpConnector.is_enabled.is_(True),
        )
    )
    if (
        not connector
        or not is_metabase(connector)
        or connector.server_url != binding.server_url
        or not connector.encrypted_credentials
    ):
        raise ReplicaReadError("replica_connection_unavailable")
    return connector


async def _rows(db, tenant_id, binding, table, filters, limit, *, now):
    connector = await _connector(db, tenant_id, binding)
    fields = TABLES[table][1]
    query = {
        "lib/type": "mbql/query",
        "stages": [
            {
                "lib/type": "mbql.stage/mbql",
                "source-table": [binding.database_name, binding.schema_name, TABLES[table][0]],
                "fields": [_field(binding, table, name) for name in fields],
                "filters": filters,
                "order-by": [["asc", {}, _field(binding, table, "id")]],
                "limit": limit,
            }
        ],
    }
    try:
        async with asyncio.timeout(40):
            result = await call_external_mcp_tool(
                connector,
                "query",
                {
                    "query": query,
                    "query_handle": None,
                    "continuation_token": None,
                },
                db,
                parse_decimal=True,
            )
        if len(str(result)) > 2_000_000:
            raise ReplicaReadError("replica_response_too_large")
        started = datetime.fromisoformat(result["started_at"])
        if (
            started.utcoffset() is None
            or not -30 <= (now - started).total_seconds() <= 120
            or result.get("status") != "completed"
            or result.get("database_id") != binding.database_id
            or result.get("cached") not in (False, None)
            or result.get("continuation_token") is not None
        ):
            raise ReplicaReadError()
        data = result["data"]
        columns, rows = data["cols"], data["rows"]
        if (
            [column["name"] for column in columns] != list(fields)
            or any(column.get("table_id") != getattr(binding, table + "_table_id") for column in columns)
            or type(result["row_count"]) is not int
            or result["row_count"] != len(rows)
            or len(rows) > limit
            or any(not isinstance(row, list) or len(row) != len(fields) for row in rows)
        ):
            raise ReplicaReadError()
        records = [dict(zip(fields, row, strict=True)) for row in rows]
        ids = [_id(row["id"]) for row in records]
        if ids != sorted(set(ids)):
            raise ReplicaReadError("replica_identity_ambiguous")
        return records
    except ReplicaReadError:
        raise
    except (KeyError, TypeError, ValueError, TimeoutError):
        raise ReplicaReadError() from None


def _order(row):
    if not isinstance(row["number"], str) or not _REFERENCE.fullmatch(row["number"]):
        raise ReplicaReadError("invalid_order_reference")
    if not isinstance(row["currency"], str) or not re.fullmatch(r"[A-Z]{3}", row["currency"]):
        raise ReplicaReadError("invalid_replica_currency")
    return {
        **row,
        "id": _id(row["id"]),
        **{key: _money(row[key]) for key in ("total", "additional_tax_total", "included_tax_total")},
        "updated_at": _source_time(row["updated_at"]),
        "completed_at": _source_time(row["completed_at"]) if row["completed_at"] else None,
    }


def _evidence(binding, now):
    return {
        "source": "framework",
        "read_at": now.isoformat(),
        "replica_freshness": "unverified",
        "provenance": {
            "provider": "metabase",
            "connector_id": str(binding.connector_id),
            "database_id": binding.database_id,
            "timestamp_storage": binding.timestamp_storage,
        },
    }


async def read_order(db, tenant_id, binding, reference, *, now=None):
    if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
        raise ReplicaReadError("invalid_order_reference")
    binding, now = _binding(binding), now or datetime.now(timezone.utc)
    rows = await _rows(
        db, tenant_id, binding, "orders", [["=", {}, _field(binding, "orders", "number"), reference]], 2, now=now
    )
    if len(rows) > 1 or any(row["number"] != reference for row in rows):
        raise ReplicaReadError("replica_identity_ambiguous")
    return {**_evidence(binding, now), "scope": "order", "page_complete": True, "orders": [_order(row) for row in rows]}


async def read_order_page(
    db, tenant_id, binding, start, end, *, after_id=0, page_size=20, basis="updated_at", now=None
):
    start, end = _window(start, end)
    if (
        type(after_id) is not int
        or not 0 <= after_id < 2**63
        or type(page_size) is not int
        or not 1 <= page_size <= 100
        or basis not in ("updated_at", "completed_at")
    ):
        raise ReplicaReadError("invalid_replica_page")
    binding, now = _binding(binding), now or datetime.now(timezone.utc)

    def field(name):
        return _field(binding, "orders", name)

    rows = await _rows(
        db,
        tenant_id,
        binding,
        "orders",
        [
            [">", {}, field("id"), after_id],
            [">=", {}, field(basis), start],
            ["<", {}, field(basis), end],
            ["not-null", {}, field("completed_at")],
        ],
        page_size + 1,
        now=now,
    )
    orders = [_order(row) for row in rows]
    for row in orders:
        observed = datetime.fromisoformat(row[basis]).replace(tzinfo=None)
        if (
            row["id"] <= after_id
            or not datetime.fromisoformat(start) <= observed < datetime.fromisoformat(end)
            or row["completed_at"] is None
        ):
            raise ReplicaReadError("replica_window_mismatch")
    more = len(orders) > page_size
    return {
        **_evidence(binding, now),
        "orders": orders[:page_size],
        "page_complete": True,
        "scan_complete": not more,
        "next_after_id": orders[page_size - 1]["id"] if more else None,
    }


async def read_payment_refunds(db, tenant_id, binding, payment_ids, *, after_id=0, now=None):
    if not isinstance(payment_ids, (list, tuple)) or not 1 <= len(payment_ids) <= 50:
        raise ReplicaReadError("invalid_payment_scope")
    payment_ids = sorted({_id(value) for value in payment_ids})
    if type(after_id) is not int or not 0 <= after_id < 2**63:
        raise ReplicaReadError("invalid_replica_page")
    binding, now = _binding(binding), now or datetime.now(timezone.utc)
    rows = await _rows(
        db,
        tenant_id,
        binding,
        "refunds",
        [
            ["=", {}, _field(binding, "refunds", "payment_id"), *payment_ids],
            [">", {}, _field(binding, "refunds", "id"), after_id],
        ],
        101,
        now=now,
    )
    refunds = []
    for row in rows:
        if _id(row["payment_id"]) not in payment_ids or _id(row["id"]) <= after_id:
            raise ReplicaReadError("replica_payment_mismatch")
        refunds.append(_refund(row))
    more = len(refunds) > 100
    return {
        **_evidence(binding, now),
        "refunds": refunds[:100],
        "page_complete": True,
        "scan_complete": not more,
        "next_after_id": refunds[99]["id"] if more else None,
    }


async def read_order_payments(db, tenant_id, binding, order_id, *, after_id=0, now=None):
    order_id = _id(order_id)
    if type(after_id) is not int or not 0 <= after_id < 2**63:
        raise ReplicaReadError("invalid_replica_page")
    binding, now = _binding(binding), now or datetime.now(timezone.utc)
    rows = await _rows(
        db,
        tenant_id,
        binding,
        "payments",
        [
            ["=", {}, _field(binding, "payments", "order_id"), order_id],
            [">", {}, _field(binding, "payments", "id"), after_id],
        ],
        101,
        now=now,
    )
    payments = []
    for row in rows:
        if _id(row["order_id"]) != order_id or _id(row["id"]) <= after_id:
            raise ReplicaReadError("replica_order_mismatch")
        payments.append(
            {
                **row,
                "id": _id(row["id"]),
                "order_id": order_id,
                "created_at": _source_time(row["created_at"]),
                "updated_at": _source_time(row["updated_at"]),
            }
        )
    more = len(payments) > 100
    return {
        **_evidence(binding, now),
        "payments": payments[:100],
        "page_complete": True,
        "scan_complete": not more,
        "next_after_id": payments[99]["id"] if more else None,
    }


def _refund(row):
    if row["transaction_id"] is not None and not isinstance(row["transaction_id"], str):
        raise ReplicaReadError("invalid_refund_evidence")
    return {
        **row,
        "id": _id(row["id"]),
        "payment_id": _id(row["payment_id"]),
        "amount": _money(row["amount"]),
        "completed": bool(row["transaction_id"]),
        "created_at": _source_time(row["created_at"]),
        "updated_at": _source_time(row["updated_at"]),
    }


async def read_refund_page(db, tenant_id, binding, start, end, *, after_id=0, now=None):
    start, end = _window(start, end)
    if type(after_id) is not int or not 0 <= after_id < 2**63:
        raise ReplicaReadError("invalid_replica_page")
    binding, now = _binding(binding), now or datetime.now(timezone.utc)

    def field(name):
        return _field(binding, "refunds", name)

    rows = await _rows(
        db,
        tenant_id,
        binding,
        "refunds",
        [
            [">", {}, field("id"), after_id],
            [">=", {}, field("updated_at"), start],
            ["<", {}, field("updated_at"), end],
        ],
        101,
        now=now,
    )
    refunds = [_refund(row) for row in rows]
    for row in refunds:
        observed = datetime.fromisoformat(row["updated_at"]).replace(tzinfo=None)
        if row["id"] <= after_id or not datetime.fromisoformat(start) <= observed < datetime.fromisoformat(end):
            raise ReplicaReadError("replica_window_mismatch")
    more = len(refunds) > 100
    return {
        **_evidence(binding, now),
        "refunds": refunds[:100],
        "page_complete": True,
        "scan_complete": not more,
        "next_after_id": refunds[99]["id"] if more else None,
    }


async def _by_ids(db, tenant_id, binding, table, identifiers, now):
    identifiers = sorted({_id(value) for value in identifiers})
    if not 1 <= len(identifiers) <= 100:
        raise ReplicaReadError("invalid_lineage_scope")
    rows = await _rows(
        db,
        tenant_id,
        binding,
        table,
        [
            ["=", {}, _field(binding, table, "id"), *identifiers],
        ],
        len(identifiers) + 1,
        now=now,
    )
    if {_id(row["id"]) for row in rows} != set(identifiers):
        raise ReplicaReadError("replica_lineage_incomplete")
    return rows


async def read_changed_refund_orders(db, tenant_id, binding, start, end, *, after_id=0, now=None):
    """One refund page + at most two parent reads, with complete ID ownership."""
    binding, now = _binding(binding), now or datetime.now(timezone.utc)
    page = await read_refund_page(db, tenant_id, binding, start, end, after_id=after_id, now=now)
    if not page["refunds"]:
        return {**page, "orders": []}
    payments = await _by_ids(db, tenant_id, binding, "payments", [row["payment_id"] for row in page["refunds"]], now)
    orders = [
        _order(row)
        for row in await _by_ids(db, tenant_id, binding, "orders", [row["order_id"] for row in payments], now)
    ]
    by_order = {row["id"]: row for row in orders}
    by_payment = {_id(row["id"]): by_order[_id(row["order_id"])] for row in payments}
    for refund in page["refunds"]:
        parent = by_payment[refund["payment_id"]]
        refund["order_id"] = parent["id"]
        refund["order_reference"] = parent["number"]
        refund["currency"] = parent["currency"]
    return {**page, "orders": orders}
