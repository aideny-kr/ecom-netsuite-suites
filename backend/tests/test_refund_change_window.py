import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.transaction_ops import refund_reader
from tests.test_solidus_refund_reader import context  # noqa: F401


async def test_refund_change_scan_finds_old_orders_without_relying_on_order_updated_at(context):  # noqa: F811
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"_id": "a" * 24, "type": "rdbms", "rdbms": {"type": "postgresql"}})
        return httpx.Response(
            200,
            json={"data": [{"id": "10", "number": "R123456789", "changed_at": "2026-09-07T12:00:00Z"}], "stages": []},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await refund_reader.read_refund_order_page(
            AsyncMock(),
            context[0],
            context[1],
            datetime(2026, 9, 7, tzinfo=timezone.utc),
            datetime(2026, 9, 8, tzinfo=timezone.utc),
            after_id=5,
            client=client,
        )
    assert result["orders"] == [{"id": "10", "number": "R123456789"}]
    assert result["next_after_id"] is None and result["page_complete"] is True
    query = json.loads(requests[1].content)["rdbms"]["query"]
    assert "r.updated_at" in query and "o.updated_at" not in query
    assert "o.id > 5" in query and "ORDER BY o.id" in query


async def test_refund_window_pages_are_bounded_and_resume_by_order_identity(monkeypatch):
    rows = [{"id": str(i), "number": f"R{i:09}", "changed_at": "2026-09-07T12:00:00Z"} for i in range(1, 102)]
    reader = AsyncMock(side_effect=[(rows, None, None), (rows[-1:], None, None)])
    monkeypatch.setattr(refund_reader, "_read_rows", reader)
    start, end = datetime(2026, 9, 7, tzinfo=timezone.utc), datetime(2026, 9, 8, tzinfo=timezone.utc)
    first = await refund_reader.read_refund_order_page(None, None, None, start, end)
    second = await refund_reader.read_refund_order_page(None, None, None, start, end, after_id=first["next_after_id"])
    assert len(first["orders"]) == 100 and first["next_after_id"] == 100
    assert second["orders"][0]["id"] == "101" and second["next_after_id"] is None


@pytest.mark.parametrize("broken", ["naive_timestamp", "outside_window", "repeated_id"])
async def test_bad_refund_cursor_or_time_evidence_is_rejected(monkeypatch, broken):
    row = {"id": "10", "number": "R123456789", "changed_at": "2026-09-07T12:00:00Z"}
    if broken == "naive_timestamp":
        row["changed_at"] = "2026-09-07T12:00:00"
    if broken == "outside_window":
        row["changed_at"] = "2026-09-01T12:00:00Z"
    if broken == "repeated_id":
        row["id"] = "5"
    monkeypatch.setattr(refund_reader, "_read_rows", AsyncMock(return_value=([row], None, None)))
    with pytest.raises(refund_reader.source.SourceReadError):
        await refund_reader.read_refund_order_page(
            None,
            None,
            None,
            datetime(2026, 9, 7, tzinfo=timezone.utc),
            datetime(2026, 9, 8, tzinfo=timezone.utc),
            after_id=5,
        )
