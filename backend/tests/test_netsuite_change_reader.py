from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.transaction_ops import netsuite_changes as reader

START = datetime(2026, 9, 6, tzinfo=timezone.utc)
END = START + timedelta(days=1)


def row(identifier):
    return {
        "id": str(identifier),
        "order_reference": f"R{identifier:09d}",
        "subsidiary": "1",
        "type": "SalesOrd",
        "modified_utc": "2026-09-06T18:21:37Z",
    }


def response(rows, more=False):
    return {
        "items": rows,
        "count": len(rows),
        "totalResults": len(rows) + (1 if more else 0),
        "hasMore": more,
        "offset": 0,
    }


@pytest.fixture
async def transport(monkeypatch):
    request = AsyncMock(return_value=response([row(i) for i in range(1, 22)], True))

    @asynccontextmanager
    async def auth(*args, **kwargs):
        assert kwargs["max_api_calls"] == 1
        yield type("Reader", (), {"request": request})()

    monkeypatch.setattr(reader, "authenticated_reader", auth)
    return request


async def read(transport, **kwargs):
    return await reader.read_changed_orders(None, uuid4(), uuid4(), "6738075", "1", "tranid", START, END, **kwargs)


@pytest.mark.asyncio
async def test_native_changed_order_page_uses_utc_half_open_window_and_sentinel(transport):
    page = await read(transport)
    assert len(page["orders"]) == 20 and page["next_after_id"] == 20 and not page["scan_complete"]
    call = transport.call_args
    assert call.kwargs["params"]["limit"] == 21
    query = call.kwargs["body"]["q"]
    assert "SYS_EXTRACT_UTC(t.lastmodifieddate)" in query and "ORDER BY t.id" in query
    assert ">=" in query and "<" in query and "2026-09-06 00:00:00" in query
    assert "l.subsidiary=1" in query


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"hasMore": True}, {"count": 0}, {"totalResults": 0}])
async def test_short_or_inconsistent_page_never_claims_complete(transport, change):
    transport.return_value = {**response([row(1)]), **change}
    with pytest.raises(reader.NetSuiteEvidenceError):
        await read(transport)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [row(2), row(1)],
        [row(1), row(1)],
        [{**row(1), "subsidiary": "2"}],
        [{**row(1), "modified_utc": "2026-09-07T00:00:00Z"}],
        [{**row(1), "order_reference": "unrelated-order"}],
    ],
)
async def test_changed_page_requires_exact_scope_and_ordered_native_ids(transport, rows):
    transport.return_value = response(rows)
    with pytest.raises(reader.NetSuiteEvidenceError):
        await read(transport)


@pytest.mark.asyncio
async def test_invalid_identifier_cannot_enter_query(transport):
    with pytest.raises(reader.NetSuiteEvidenceError):
        await reader.read_changed_orders(None, uuid4(), uuid4(), "6738075", "1 OR 1=1", "tranid", START, END)
    transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_change_reader_rejects_other_tenant_before_auth_or_network(db, admin_user, tenant_b):
    from tests.test_transaction_defaults import connections

    _, target = await connections(db, admin_user[0].tenant_id)
    with pytest.raises(reader.NetSuiteEvidenceError, match="invalid_connection"):
        await reader.read_changed_orders(db, tenant_b.id, target.id, "6738075", "1", "tranid", START, END)
