"""Bounded, resumable Solidus order mirror. No upstream mutations or absence claims."""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError

from app.core.database import set_tenant_context
from app.models.canonical import Order
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.models.pipeline import CursorState
from app.models.tenant import Tenant
from app.schemas.transaction_ops import _decimal
from app.services import audit_service
from app.services.ingestion.base import save_cursor_async
from app.services.transaction_ops.source_reader import SourceReadError, read_framework_orders_page

CURSOR_TYPE = "solidus_orders_v1"
INITIAL_LOOKBACK_DAYS = 7
MAX_PAGES = 50
DEADLINE_SECONDS = 180
_REFERENCE = re.compile(r"R[0-9]{9}(?:-[A-Z0-9]+)?\Z")
_HEADER_FIELDS = (
    "id",
    "number",
    "currency",
    "total",
    "item_total",
    "included_tax_total",
    "additional_tax_total",
    "tax_total",
    "created_at",
    "updated_at",
    "completed_at",
    "state",
)


class SolidusImportError(RuntimeError):
    """Only safe reason codes cross the job/API boundary."""


def _time(value):
    if not isinstance(value, str):
        raise ValueError("Missing source timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("Naive source timestamp")
    return parsed.astimezone(timezone.utc)


def _money(value):
    if value is None:
        return None
    amount = _decimal(value, Decimal("1e18"))
    # The mirror preserves signed source evidence; repair eligibility belongs to
    # reconciliation. Rejecting a negative order would strand the entire cursor.
    if amount != amount.quantize(Decimal("0.000001")):
        raise ValueError("Unsupported source amount")
    return amount


def project_canonical_order(order, tenant_id, connection_id, observed_at):
    try:
        reference, currency, source_id = order.get("number"), order.get("currency"), str(order.get("id", ""))
        if (
            not isinstance(reference, str)
            or not _REFERENCE.fullmatch(reference)
            or len(reference) > 100
            or not re.fullmatch(r"[0-9]{1,50}", source_id)
            or not isinstance(currency, str)
            or not re.fullmatch(r"[A-Z]{3}", currency)
        ):
            raise ValueError("Invalid source identity")
        total = _money(order.get("total"))
        state = order.get("state")
        if total is None or not isinstance(state, str) or not 1 <= len(state) <= 50:
            raise ValueError("Missing source amount or state")
        included, additional = _money(order.get("included_tax_total")), _money(order.get("additional_tax_total"))
        tax = included + additional if included is not None and additional is not None else None
        if "tax_total" in order and _money(order["tax_total"]) != tax:
            tax = None
        header = {key: order[key] for key in _HEADER_FIELDS if key in order}
        entity = order.get("business_entity")
        if isinstance(entity, dict) and str(entity.get("id", "")).isdigit():
            header["business_entity"] = {"id": str(entity["id"])}
        elif isinstance(entity, str) and 0 < len(entity) <= 100:
            header["business_entity"] = entity
        elif "business_entity" in order and entity is None:
            header["business_entity"] = None
        return {
            "tenant_id": tenant_id,
            "dedupe_key": f"solidus:{connection_id}:{source_id}",
            "source": "solidus",
            "source_id": source_id,
            "source_connection_id": connection_id,
            "order_number": reference,
            "currency": currency,
            "total_amount": total,
            "subtotal": _money(order.get("item_total")),
            "tax_amount": tax,
            "discount_amount": None,
            "status": state,
            "source_created_at": _time(order["created_at"]) if order.get("created_at") else None,
            "source_updated_at": _time(order.get("updated_at")),
            "updated_at": observed_at,
            "raw_data": {"order": header, "observed_at": observed_at.isoformat()},
        }
    except (ValueError, TypeError, AttributeError, DecimalException):
        raise SolidusImportError("invalid_source_order") from None


async def _upsert_order(db, row):
    statement = insert(Order).values(**row)
    await db.execute(
        statement.on_conflict_do_update(
            constraint="uq_orders_dedupe",
            set_={key: statement.excluded[key] for key in row if key not in {"tenant_id", "dedupe_key"}},
            where=and_(
                Order.tenant_id == row["tenant_id"],
                Order.source_connection_id == row["source_connection_id"],
                or_(
                    Order.source_updated_at.is_(None),
                    Order.source_updated_at < statement.excluded.source_updated_at,
                    and_(
                        Order.source_updated_at == statement.excluded.source_updated_at,
                        Order.updated_at <= statement.excluded.updated_at,
                    ),
                ),
            ),
        )
    )


async def save_observed_order(db, tenant_id, connection_id, order, observed_at):
    """Mirror an exact investigation read, including older refunded orders."""
    row = project_canonical_order(order, tenant_id, connection_id, observed_at)
    await set_tenant_context(db, tenant_id)
    connection = await db.scalar(
        select(Connection.id)
        .join(Tenant, Tenant.id == Connection.tenant_id)
        .where(
            Connection.id == connection_id,
            Connection.tenant_id == tenant_id,
            Connection.provider == "solidus",
            Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
            Connection.metadata_json["api_profile"].astext == "framework_sync",
            Tenant.is_active.is_(True),
        )
        .with_for_update(of=Connection, read=True)
    )
    if connection is None:
        raise SolidusImportError("source_unavailable")
    await _upsert_order(db, row)
    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="ingestion",
        action="solidus.order.observed",
        actor_type="system",
        resource_type="connection",
        resource_id=str(connection_id),
        payload={"order_reference": row["order_number"], "source_updated_at": row["source_updated_at"].isoformat()},
    )
    # Release the connection lock before any subsequent remote investigation reads.
    await db.commit()


def _new_scan(state, now):
    watermark = state.get("watermark")
    since = _time(watermark) - timedelta(hours=1) if watermark else now - timedelta(days=INITIAL_LOOKBACK_DAYS)
    return {
        **{key: state[key] for key in ("watermark", "completed_at") if key in state},
        "since": since.isoformat(),
        "started_at": now.isoformat(),
        "next_page": 1,
        "seen": 0,
    }


async def _restart_scan(db, connection_id, state, calls):
    state.update(next_page=1, seen=0)
    for key in ("total", "last_reference", "last_source_id"):
        state.pop(key, None)
    await save_cursor_async(db, connection_id, CURSOR_TYPE, json.dumps(state, separators=(",", ":")))
    await db.commit()
    return {
        "termination_reason": "stall",
        "reason": "source_window_changed",
        "complete": False,
        "records_synced": 0,
        "api_calls": calls,
    }


async def _sync_page(db, tenant_id, connection_id, now):
    await set_tenant_context(db, tenant_id)
    # Serialize import cursors independently of connection availability checks.
    # Shared connection locks still block revocation, but allow investigations to
    # mirror unrelated orders while a bulk page is being read.
    acquired = await db.scalar(
        select(func.pg_try_advisory_xact_lock(func.hashtextextended(f"solidus_orders:{tenant_id}:{connection_id}", 0)))
    )
    if not acquired:
        await db.rollback()
        return {
            "termination_reason": "stall",
            "reason": "refresh_in_progress",
            "complete": False,
            "records_synced": 0,
            "api_calls": 0,
        }
    connection = await db.scalar(
        select(Connection)
        .join(Tenant, Tenant.id == Connection.tenant_id)
        .where(
            Connection.id == connection_id,
            Connection.tenant_id == tenant_id,
            Connection.provider == "solidus",
            Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
            Tenant.is_active.is_(True),
        )
        .with_for_update(of=Connection, read=True, skip_locked=True)
    )
    if connection is None:
        exists = await db.scalar(
            select(Connection.id)
            .where(
                Connection.id == connection_id,
                Connection.tenant_id == tenant_id,
                Connection.provider == "solidus",
                Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
            )
            .join(Tenant, Tenant.id == Connection.tenant_id)
            .where(Tenant.is_active.is_(True))
        )
        if exists:
            await db.rollback()
            return {
                "termination_reason": "stall",
                "reason": "refresh_in_progress",
                "complete": False,
                "records_synced": 0,
                "api_calls": 0,
            }
        raise SolidusImportError("source_unavailable")
    cursor = await db.scalar(
        select(CursorState.cursor_value).where(
            CursorState.connection_id == connection_id,
            CursorState.object_type == CURSOR_TYPE,
        )
    )
    state = json.loads(cursor) if cursor else {}
    if not state.get("next_page"):
        state = _new_scan(state, now)
    page = state["next_page"]
    # A fixed upper watermark plus source-ID keyset avoids offset shifts. Updates
    # after this watermark are picked up by the next overlapping refresh.
    evidence = await read_framework_orders_page(
        db,
        tenant_id,
        None,
        _time(state["since"]),
        page=1,
        source_connection_id=connection_id,
        after_id=int(state.get("last_source_id", "0")),
        updated_before=_time(state["started_at"]),
    )
    calls = 1
    observed = _time(evidence["read_at"])
    rows = [project_canonical_order(order, tenant_id, connection_id, observed) for order in evidence["orders"]]
    identities = [int(row["source_id"]) for row in rows]
    previous = int(state.get("last_source_id", "0")) if page > 1 else 0
    if identities != sorted(set(identities)) or (identities and identities[0] <= previous):
        return await _restart_scan(db, connection_id, state, calls)
    if any(not _time(state["since"]) <= row["source_updated_at"] <= _time(state["started_at"]) for row in rows):
        raise SolidusImportError("source_window_mismatch")
    for row in rows:
        await _upsert_order(db, row)
    next_page = page + 1 if evidence["next_page"] is not None else None
    seen = state.get("seen", 0)
    state.update(next_page=next_page, total=seen + evidence["total_count"], seen=seen + len(rows))
    if rows:
        state["last_source_id"] = rows[-1]["source_id"]
    if next_page is None:
        state.update(watermark=state["started_at"], completed_at=observed.isoformat())
    await save_cursor_async(db, connection_id, CURSOR_TYPE, json.dumps(state, separators=(",", ":")))
    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="ingestion",
        action="solidus.orders.page",
        actor_type="system",
        resource_type="connection",
        resource_id=str(connection_id),
        payload={"page": page, "records_synced": len(rows), "complete": next_page is None},
    )
    await db.commit()
    return {
        "termination_reason": "done" if next_page is None else "budget",
        "reason": None,
        "complete": next_page is None,
        "records_synced": len(rows),
        "api_calls": calls,
        "coverage_since": state["since"],
        "next_page": next_page,
        "source_total": state["total"],
    }


async def sync_solidus_orders(db, tenant_id, connection_id, *, now=None, max_pages=MAX_PAGES):
    tenant_id, connection_id = UUID(str(tenant_id)), UUID(str(connection_id))
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None or type(max_pages) is not int or not 1 <= max_pages <= MAX_PAGES:
        raise SolidusImportError("invalid_import_budget")
    records, calls, pages_read = 0, 0, 0
    try:
        async with asyncio.timeout(DEADLINE_SECONDS):
            for _ in range(max_pages):
                result = await _sync_page(db, tenant_id, connection_id, now)
                records += result["records_synced"]
                calls += result["api_calls"]
                pages_read += 1
                if result["termination_reason"] != "budget":
                    break
        return {**result, "records_synced": records, "api_calls": calls, "pages_read": pages_read}
    except TimeoutError:
        await db.rollback()
        return {
            "termination_reason": "budget",
            "reason": "deadline",
            "complete": False,
            "records_synced": records,
            "api_calls": calls,
            "pages_read": pages_read,
        }
    except SourceReadError as exc:
        await db.rollback()
        raise SolidusImportError(exc.code) from None
    except SolidusImportError:
        await db.rollback()
        raise
    except (DBAPIError, ValueError, KeyError, TypeError):
        await db.rollback()
        raise SolidusImportError("import_failed") from None
