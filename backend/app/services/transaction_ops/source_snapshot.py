"""Reuse a real detail observation within a bounded scan, never in a write preflight.

Page/header mirrors and replica rows are not detail evidence. No timestamp is
refreshed on a hit; later scans, known newer versions, credential changes and
expired observations require a new source read. Refunds have their own reader.
"""

import copy
import hashlib
import json
from datetime import timedelta

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert

from app.core.database import set_tenant_context
from app.models.canonical import Order
from app.models.tenant import Tenant
from app.models.transaction_source_snapshot import TransactionSourceSnapshot
from app.services.transaction_ops.normalization import _time
from app.services.transaction_ops.source_projection import ProjectionError, project_order
from app.services.transaction_ops.source_reader import SourceReadError, direct_connection

MAX_AGE = timedelta(hours=24)
MAX_BYTES = 512_000
VERSION = 1


def _timestamp(value):
    try:
        return _time(value)
    except (ValueError, TypeError, AttributeError):
        return None


def scan_floor(run, now):
    """A continuation retains its cycle's start; a new daily/manual scan does not."""
    if (
        getattr(run, "origin", None) == "recovery"
        or run.params_json.get("order_references")
        or not run.params_json.get("window_start")
        or not run.params_json.get("window_end")
    ):
        return None
    started = _timestamp((run.progress_json or {}).get("continuation_started_at")) or getattr(run, "created_at", None)
    if started is None or started.utcoffset() is None or started > now:
        return None
    return max(started, now - MAX_AGE)


def _project(evidence, connection_id, reference):
    if (
        not isinstance(evidence, dict)
        or evidence.get("source") != "framework"
        or evidence.get("source_transport") != "solidus_direct"
        or evidence.get("connection_id") != str(connection_id)
        or evidence.get("scope") != "order"
        or evidence.get("page_complete") is not True
        or not isinstance(evidence.get("orders"), list)
        or len(evidence["orders"]) != 1
    ):
        return None
    order = evidence["orders"][0]
    if not isinstance(order, dict) or order.get("number") != reference or "business_entity" not in order:
        return None
    observed, updated = _timestamp(evidence.get("read_at")), _timestamp(order.get("updated_at"))
    if observed is None or updated is None or updated > observed or not str(order.get("id", "")).isdigit():
        return None
    try:
        projected = project_order(order)
        # Re-projection of the sanitized object must preserve its tax geography.
        if "tax_jurisdiction" in order:
            projected = project_order({**order, "ship_address": order["tax_jurisdiction"]})
            if order["tax_jurisdiction"] == {"problem": "invalid_source_tax_jurisdiction"}:
                projected["tax_jurisdiction"] = order["tax_jurisdiction"]
        result = {
            "source": "framework",
            "source_transport": "solidus_direct",
            "connection_id": str(connection_id),
            "scope": "order",
            "read_at": evidence["read_at"],
            "orders": [projected],
            "page_complete": True,
            "window_complete": False,
            "next_page": None,
        }
        if len(json.dumps(result).encode()) > MAX_BYTES:
            return None
        return result
    except (ProjectionError, TypeError, ValueError):
        return None


async def _connection(db, tenant_id, connection_id):
    await set_tenant_context(db, tenant_id)
    if not await db.scalar(select(Tenant.id).where(Tenant.id == tenant_id, Tenant.is_active.is_(True))):
        raise SourceReadError("source_not_found", 404)
    connection, _ = await direct_connection(db, tenant_id, connection_id)
    # Never store credentials in the snapshot. Rotation invalidates prior scope.
    return hashlib.sha256(connection.encrypted_credentials.encode()).hexdigest()


async def load(db, tenant_id, connection_id, reference, *, since, now, minimum_version=None):
    fingerprint = await _connection(db, tenant_id, connection_id)
    row = await db.scalar(
        select(TransactionSourceSnapshot)
        .execution_options(populate_existing=True)
        .where(
            TransactionSourceSnapshot.tenant_id == tenant_id,
            TransactionSourceSnapshot.connection_id == connection_id,
            TransactionSourceSnapshot.order_reference == reference,
            TransactionSourceSnapshot.connection_fingerprint == fingerprint,
            TransactionSourceSnapshot.observed_at >= max(since, now - MAX_AGE),
            TransactionSourceSnapshot.observed_at <= now,
        )
    )
    if row is None or (minimum_version is not None and row.source_updated_at < minimum_version):
        return None
    # An incremental mirror may already know the saved detail is obsolete.
    newer = await db.scalar(
        select(Order.id)
        .where(
            Order.tenant_id == tenant_id,
            Order.source_connection_id == connection_id,
            Order.source == "solidus",
            Order.order_number == reference,
            Order.source_updated_at > row.source_updated_at,
        )
        .limit(1)
    )
    if newer or not isinstance(row.evidence_json, dict) or row.evidence_json.get("version") != VERSION:
        return None
    evidence = _project(row.evidence_json.get("evidence"), connection_id, reference)
    if evidence is None or _time(evidence["read_at"]) != row.observed_at:
        return None
    if _time(evidence["orders"][0]["updated_at"]) != row.source_updated_at:
        return None
    return copy.deepcopy(evidence)


async def save(db, tenant_id, connection_id, reference, evidence, *, now):
    projected = _project(evidence, connection_id, reference)
    if projected is None or not now - MAX_AGE <= _time(projected["read_at"]) <= now:
        return False
    fingerprint = await _connection(db, tenant_id, connection_id)
    if evidence.get("_connection_fingerprint") != fingerprint:
        return False
    statement = insert(TransactionSourceSnapshot).values(
        tenant_id=tenant_id,
        connection_id=connection_id,
        order_reference=reference,
        connection_fingerprint=fingerprint,
        observed_at=_time(projected["read_at"]),
        source_updated_at=_time(projected["orders"][0]["updated_at"]),
        evidence_json={"version": VERSION, "evidence": projected},
        updated_at=now,
    )
    await db.execute(
        statement.on_conflict_do_update(
            constraint="uq_tx_source_snapshot",
            set_={
                key: getattr(statement.excluded, key)
                for key in (
                    "connection_fingerprint",
                    "observed_at",
                    "source_updated_at",
                    "evidence_json",
                    "updated_at",
                )
            },
            where=and_(
                TransactionSourceSnapshot.observed_at <= statement.excluded.observed_at,
                TransactionSourceSnapshot.source_updated_at <= statement.excluded.source_updated_at,
            ),
        )
    )
    await db.commit()
    return True
