"""Solidus imports preserve evidence, resume safely, and never write upstream."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.canonical import Order
from app.models.connection import Connection
from app.models.pipeline import CursorState
from app.services.ingestion import solidus_sync as sync

NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def source_order(reference="R100120031", **changes):
    return {
        "id": reference[1:],
        "number": reference,
        "currency": "USD",
        "total": "1724.00",
        "item_total": "1600.00",
        "included_tax_total": "0.00",
        "additional_tax_total": "124.00",
        "state": "complete",
        "created_at": "2026-09-06T10:00:00Z",
        "updated_at": "2026-09-07T12:00:00Z",
        "completed_at": "2026-09-06T10:30:00Z",
        "business_entity": {"id": "1", "name": "Framework"},
        **changes,
    }


def test_source_amounts_are_exact_and_tax_is_not_added_twice():
    row = sync.project_canonical_order(source_order(), uuid.uuid4(), uuid.uuid4(), NOW)
    assert row["total_amount"] == Decimal("1724.00")
    assert row["tax_amount"] == Decimal("124.00")
    assert row["source_created_at"].isoformat() == "2026-09-06T10:00:00+00:00"
    assert row["discount_amount"] is None
    assert "customer_email" not in row


def test_missing_tax_stays_unknown_and_customer_payload_is_not_stored():
    order = source_order(customer_email="private@example.com", auth={"secret": "private"})
    del order["additional_tax_total"]
    row = sync.project_canonical_order(order, uuid.uuid4(), uuid.uuid4(), NOW)
    assert row["tax_amount"] is None
    assert "private" not in str(row)
    assert "refund" not in row["raw_data"]


def test_tax_included_in_price_and_six_decimal_amounts_are_preserved():
    row = sync.project_canonical_order(
        source_order(total="100.123456", included_tax_total="20.123456", additional_tax_total="0"),
        uuid.uuid4(),
        uuid.uuid4(),
        NOW,
    )
    assert row["total_amount"] == Decimal("100.123456")
    assert row["tax_amount"] == Decimal("20.123456")


@pytest.mark.parametrize(
    "changes",
    [
        {"total": 12.3},
        {"total": "NaN"},
        {"total": "-1"},
        {"total": "1.0000001"},
        {"total": "1E99999"},
        {"currency": "usd"},
        {"number": "bad"},
        {"updated_at": "2026-09-07T10:00:00"},
        {"state": ""},
    ],
)
def test_invalid_financial_rows_are_not_silently_coerced(changes):
    with pytest.raises(sync.SolidusImportError):
        sync.project_canonical_order(source_order(**changes), uuid.uuid4(), uuid.uuid4(), NOW)


def test_conflicting_tax_evidence_stays_unknown():
    row = sync.project_canonical_order(source_order(tax_total="999"), uuid.uuid4(), uuid.uuid4(), NOW)
    assert row["tax_amount"] is None


def test_scalar_source_entity_is_preserved_for_verified_subsidiary_routing():
    row = sync.project_canonical_order(source_order(business_entity="Framework BV"), uuid.uuid4(), uuid.uuid4(), NOW)
    assert row["raw_data"]["order"]["business_entity"] == "Framework BV"


def test_oversized_reference_cannot_overflow_a_durable_cursor():
    with pytest.raises(sync.SolidusImportError):
        sync.project_canonical_order(source_order(number="R100120031-" + "A" * 240), uuid.uuid4(), uuid.uuid4(), NOW)


async def connection(db, tenant_id, **changes):
    row = Connection(
        tenant_id=tenant_id,
        provider="solidus",
        label="Solidus",
        status="active",
        encrypted_credentials="unused-by-test-reader",
        **changes,
    )
    db.add(row)
    await db.flush()
    return row


def fake_pages(monkeypatch, orders, *, page_size=20):
    calls = []

    async def read(db, tenant_id, step_id, updated_since, page=1, **kwargs):
        calls.append({"page": page, "since": updated_since, "tenant": tenant_id, **kwargs})
        eligible = [row for row in orders if int(row["id"]) > kwargs.get("after_id", 0)]
        offset = (page - 1) * page_size
        return {
            "source": "framework",
            "read_at": NOW.isoformat(),
            "page_complete": True,
            "page": page,
            "total_count": len(eligible),
            "orders": eligible[offset : offset + page_size],
            "next_page": page + 1 if offset + page_size < len(eligible) else None,
        }

    monkeypatch.setattr(sync, "read_framework_orders_page", read)
    return calls


async def test_import_is_idempotent_and_persists_original_dates_and_unknown_tax(db, admin_user, monkeypatch):
    user, _ = admin_user
    conn = await connection(db, user.tenant_id)
    orders = [source_order(additional_tax_total=None)]
    calls = fake_pages(monkeypatch, orders)
    first = await sync.sync_solidus_orders(db, user.tenant_id, conn.id, now=NOW)
    second = await sync.sync_solidus_orders(db, user.tenant_id, conn.id, now=NOW)
    rows = (await db.scalars(select(Order).where(Order.tenant_id == user.tenant_id))).all()
    assert first["termination_reason"] == second["termination_reason"] == "done"
    assert len(rows) == 1 and rows[0].tax_amount is None
    assert rows[0].source_connection_id == conn.id
    assert rows[0].source_created_at.day == 6
    assert calls[0]["source_connection_id"] == conn.id


async def test_budgeted_import_resumes_without_claiming_complete(db, admin_user, monkeypatch):
    user, _ = admin_user
    conn = await connection(db, user.tenant_id)
    orders = [source_order(f"R10012{i:004}") for i in range(21)]
    fake_pages(monkeypatch, orders)
    first = await sync.sync_solidus_orders(db, user.tenant_id, conn.id, now=NOW, max_pages=1)
    cursor = await db.scalar(select(CursorState).where(CursorState.connection_id == conn.id))
    assert first["termination_reason"] == "budget" and first["complete"] is False
    assert first["records_synced"] == 20
    assert '"next_page":2' in cursor.cursor_value
    second = await sync.sync_solidus_orders(db, user.tenant_id, conn.id, now=NOW, max_pages=2)
    assert second["complete"] is True
    assert len((await db.scalars(select(Order).where(Order.tenant_id == user.tenant_id))).all()) == 21


async def test_changing_population_does_not_restart_or_skip_unread_ids(db, admin_user, monkeypatch):
    user, _ = admin_user
    conn = await connection(db, user.tenant_id)
    orders = [source_order(f"R10012{i:004}") for i in range(21)]
    fake_pages(monkeypatch, orders)
    await sync.sync_solidus_orders(db, user.tenant_id, conn.id, now=NOW, max_pages=1)
    orders.append(source_order("R999999999"))
    result = await sync.sync_solidus_orders(db, user.tenant_id, conn.id, now=NOW)
    assert result["termination_reason"] == "done"
    assert len((await db.scalars(select(Order).where(Order.tenant_id == user.tenant_id))).all()) == 22


async def test_other_tenant_cannot_import_even_when_database_role_bypasses_rls(
    db, admin_user, admin_user_b, monkeypatch
):
    conn = await connection(db, admin_user_b[0].tenant_id)
    calls = fake_pages(monkeypatch, [source_order()])
    with pytest.raises(sync.SolidusImportError, match="source_unavailable"):
        await sync.sync_solidus_orders(db, admin_user[0].tenant_id, conn.id, now=NOW)
    assert calls == []


async def test_source_failure_does_not_advance_cursor_or_store_raw_errors(db, admin_user, monkeypatch):
    user, _ = admin_user
    conn = await connection(db, user.tenant_id)

    async def failure(*args, **kwargs):
        raise sync.SourceReadError("source_rate_limited", 429)

    monkeypatch.setattr(sync, "read_framework_orders_page", failure)
    with pytest.raises(sync.SolidusImportError, match="source_rate_limited"):
        await sync.sync_solidus_orders(db, user.tenant_id, conn.id, now=NOW)
    assert await db.scalar(select(CursorState).where(CursorState.connection_id == conn.id)) is None


async def test_multiple_pages_in_one_job_use_the_committed_cursor(db, admin_user, monkeypatch):
    user, _ = admin_user
    conn = await connection(db, user.tenant_id)
    fake_pages(monkeypatch, [source_order(f"R10012{i:004}") for i in range(61)])
    result = await sync.sync_solidus_orders(db, user.tenant_id, conn.id, now=NOW, max_pages=4)
    assert result["complete"] is True and result["records_synced"] == 61
    assert len((await db.scalars(select(Order).where(Order.tenant_id == user.tenant_id))).all()) == 61


async def test_duplicate_source_identity_across_pages_does_not_complete_scan(db, admin_user, monkeypatch):
    user, _ = admin_user
    conn = await connection(db, user.tenant_id)
    orders = [source_order(f"R10012{i:004}") for i in range(20)]
    fake_pages(monkeypatch, [*orders, orders[-1]])
    original = sync.read_framework_orders_page

    async def ignores_cursor(*args, **kwargs):
        kwargs.pop("after_id", None)
        return await original(*args, **kwargs)

    monkeypatch.setattr(sync, "read_framework_orders_page", ignores_cursor)
    result = await sync.sync_solidus_orders(db, user.tenant_id, conn.id, now=NOW)
    assert result["termination_reason"] == "stall" and result["complete"] is False
    assert result["reason"] == "source_window_changed"


@pytest.mark.parametrize("status", ["error", "revoked", "superseded"])
async def test_inactive_connections_cannot_be_read(db, admin_user, monkeypatch, status):
    user, _ = admin_user
    conn = await connection(db, user.tenant_id)
    conn.status = status
    await db.flush()
    calls = fake_pages(monkeypatch, [source_order()])
    with pytest.raises(sync.SolidusImportError, match="source_unavailable"):
        await sync.sync_solidus_orders(db, user.tenant_id, conn.id, now=NOW)
    assert calls == []


async def test_failed_later_page_keeps_committed_records_and_resume_cursor(db, admin_user, monkeypatch):
    user, _ = admin_user
    conn = await connection(db, user.tenant_id)
    tenant_id, connection_id = user.tenant_id, conn.id
    fake_pages(monkeypatch, [source_order(f"R10012{i:004}") for i in range(21)])
    original = sync.read_framework_orders_page

    async def read(*args, **kwargs):
        if kwargs.get("after_id", 0) > 0:
            raise sync.SourceReadError("source_timeout")
        return await original(*args, **kwargs)

    monkeypatch.setattr(sync, "read_framework_orders_page", read)
    with pytest.raises(sync.SolidusImportError, match="source_timeout"):
        await sync.sync_solidus_orders(db, tenant_id, connection_id, now=NOW)
    cursor = await db.scalar(select(CursorState.cursor_value).where(CursorState.connection_id == connection_id))
    assert '"next_page":2' in cursor and '"watermark"' not in cursor
    assert len((await db.scalars(select(Order).where(Order.tenant_id == tenant_id))).all()) == 20


async def test_six_decimal_amounts_survive_database_round_trip(db, admin_user, monkeypatch):
    user, _ = admin_user
    conn = await connection(db, user.tenant_id)
    fake_pages(monkeypatch, [source_order(total="100.123456")])
    await sync.sync_solidus_orders(db, user.tenant_id, conn.id, now=NOW)
    row = await db.scalar(select(Order).where(Order.tenant_id == user.tenant_id))
    assert row.total_amount == Decimal("100.123456")
