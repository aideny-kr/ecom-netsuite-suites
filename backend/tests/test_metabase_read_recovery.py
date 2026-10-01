"""Recheck fixed replica reads without accepting incomplete or mismatched proof."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.transaction_ops import metabase_reader as reader
from app.services.transaction_ops.read_recovery import ReadBudgetExhaustedError, read_with_recovery
from tests.test_metabase_replica_reader import BINDING, NOW, order_row, result


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"status": "running"}, "replica_query_incomplete"),
        ({"status": "pending"}, "replica_query_incomplete"),
        ({"cached": True}, "replica_cached_response"),
        ({"started_at": "2026-09-01T00:00:00Z"}, "replica_read_not_fresh"),
    ],
)
async def test_temporary_response_is_rechecked_at_same_cursor_with_metered_retry(monkeypatch, overrides, code):
    row = order_row(id=43)
    valid = result(reader.ORDER_FIELDS, [[row[f] for f in reader.ORDER_FIELDS]])
    query = AsyncMock(side_effect=[{**valid, **overrides}, valid])
    monkeypatch.setattr(reader, "_connector", AsyncMock(return_value=object()))
    monkeypatch.setattr(reader, "call_external_mcp_tool", query)
    progress = {"last_source_id": 42, "pending_refs": ["R100000001"]}
    reserve, save, sleep = AsyncMock(return_value=True), AsyncMock(), AsyncMock()
    page = await read_with_recovery(
        lambda: reader.read_order_page(
            AsyncMock(), uuid4(), BINDING, NOW.replace(day=7, hour=0), NOW, after_id=42, now=NOW
        ),
        retry_calls=6,
        progress=progress,
        reserve=reserve,
        save=save,
        remaining=lambda: 60,
        sleep=sleep,
        stage="source_page",
    )
    assert len(page["orders"]) == 1
    assert query.await_count == 2
    assert query.call_args_list[0].args[2] == query.call_args_list[1].args[2]
    reserve.assert_awaited_once_with(6)
    assert progress["last_source_id"] == 42 and progress["pending_refs"] == ["R100000001"]
    assert progress["last_read_failure"]["code"] == code
    assert progress["last_read_failure"]["resolved"] is True


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"database_id": 100}, "replica_database_mismatch"),
        ({"row_count": 2}, "replica_response_invalid"),
        ({"continuation_token": "private-token"}, "replica_page_incomplete"),
        ({"status": "unknown-private-status"}, "replica_response_invalid"),
        ({"status": "failed", "error": "private query error"}, "replica_query_failed"),
    ],
)
async def test_invalid_proof_stays_blocked_with_specific_sanitized_reason(monkeypatch, overrides, code):
    row = order_row()
    query = AsyncMock(return_value=result(reader.ORDER_FIELDS, [[row[f] for f in reader.ORDER_FIELDS]], **overrides))
    monkeypatch.setattr(reader, "_connector", AsyncMock(return_value=object()))
    monkeypatch.setattr(reader, "call_external_mcp_tool", query)
    progress = {"last_source_id": 42}
    reserve = AsyncMock()
    with pytest.raises(reader.ReplicaReadError, match=code):
        await read_with_recovery(
            lambda: reader.read_order(AsyncMock(), uuid4(), BINDING, row["number"], now=NOW),
            retry_calls=6,
            progress=progress,
            reserve=reserve,
            save=AsyncMock(),
            remaining=lambda: 60,
            sleep=AsyncMock(),
            stage="source_page",
        )
    reserve.assert_not_awaited()
    query.assert_awaited_once()
    assert progress["last_read_failure"]["code"] == code
    assert progress["last_read_failure"]["retryable"] is False
    assert progress["last_source_id"] == 42
    assert "private" not in str(progress)


async def test_persistent_incomplete_query_stops_at_existing_retry_limit(monkeypatch):
    from types import SimpleNamespace

    from app.services.transaction_ops.continuation import scheduled_read_stop

    query = AsyncMock(return_value=result(reader.ORDER_FIELDS, [], status="pending"))
    monkeypatch.setattr(reader, "_connector", AsyncMock(return_value=object()))
    monkeypatch.setattr(reader, "call_external_mcp_tool", query)
    run_id = uuid4()
    progress = {"last_source_id": 42, "read_retry_count": 3}
    reserve = AsyncMock()
    with pytest.raises(ReadBudgetExhaustedError):
        await read_with_recovery(
            lambda: reader.read_order_page(AsyncMock(), uuid4(), BINDING, NOW.replace(day=7, hour=0), NOW, now=NOW),
            retry_calls=6,
            progress=progress,
            reserve=reserve,
            save=AsyncMock(),
            remaining=lambda: 60,
            sleep=AsyncMock(),
            stage="source_page",
            run_id=run_id,
        )
    query.assert_awaited_once()
    reserve.assert_not_awaited()
    assert progress["read_stop_reason"] == "retry_limit"
    assert progress["last_source_id"] == 42
    assert scheduled_read_stop(
        SimpleNamespace(
            id=run_id, origin="schedule", status="finished", termination_reason="budget", progress_json=progress
        )
    )


@pytest.mark.parametrize("transient", [False, True])
async def test_token_refresh_outage_retries_but_missing_authorization_stays_blocked(monkeypatch, transient):
    from app.services.mcp_client_service import McpAuthenticationError
    from app.services.metabase_oauth_service import OAuthError

    row = order_row()
    valid = result(reader.ORDER_FIELDS, [[row[f] for f in reader.ORDER_FIELDS]])
    error = (
        OAuthError("safe message", code="metabase_upstream_unavailable")
        if transient
        else McpAuthenticationError("private credential error")
    )
    query = AsyncMock(side_effect=[error, valid])
    monkeypatch.setattr(reader, "_connector", AsyncMock(return_value=object()))
    monkeypatch.setattr(reader, "call_external_mcp_tool", query)
    progress, reserve = {"last_source_id": 42}, AsyncMock(return_value=True)

    async def run():
        return await read_with_recovery(
            lambda: reader.read_order(AsyncMock(), uuid4(), BINDING, row["number"], now=NOW),
            retry_calls=6,
            progress=progress,
            reserve=reserve,
            save=AsyncMock(),
            remaining=lambda: 60,
            sleep=AsyncMock(),
            stage="source_page",
        )

    if transient:
        assert len((await run())["orders"]) == 1
        assert query.await_count == 2
        reserve.assert_awaited_once_with(6)
        assert progress["last_read_failure"]["resolved"] is True
    else:
        with pytest.raises(reader.ReplicaReadError, match="replica_authentication_required"):
            await run()
        query.assert_awaited_once()
        reserve.assert_not_awaited()
        assert progress["last_read_failure"]["resolved"] is False
    assert "private" not in str(progress)
    assert progress["last_source_id"] == 42


@pytest.mark.parametrize("code", ["private upstream body", {"error": "private"}, ["private"]])
async def test_unknown_replica_failure_code_is_never_persisted_or_retried(code):
    progress, reserve = {}, AsyncMock()
    with pytest.raises(reader.ReplicaReadError):
        await read_with_recovery(
            AsyncMock(side_effect=reader.ReplicaReadError(code)),
            retry_calls=6,
            progress=progress,
            reserve=reserve,
            save=AsyncMock(),
            remaining=lambda: 60,
        )
    reserve.assert_not_awaited()
    assert progress["last_read_failure"]["code"] == "unclassified_read_failure"
    assert "private" not in str(progress)


async def test_cached_old_result_is_diagnosed_as_cache_not_clock_failure(monkeypatch):
    query = AsyncMock(return_value=result(reader.ORDER_FIELDS, [], cached=True, started_at="2026-09-01T00:00:00Z"))
    monkeypatch.setattr(reader, "_connector", AsyncMock(return_value=object()))
    monkeypatch.setattr(reader, "call_external_mcp_tool", query)
    with pytest.raises(reader.ReplicaReadError, match="replica_cached_response"):
        await reader.read_order(AsyncMock(), uuid4(), BINDING, "R100000001", now=NOW)


async def test_refund_parent_read_retry_reserves_full_three_query_cost(monkeypatch):
    row = order_row(id=43)
    refund = result(
        reader.REFUND_FIELDS,
        [[7, 8, "100.01", "ch_refund_7", "2026-09-07T00:00:00Z", "2026-09-07T01:00:00Z", None, 1]],
        table="refunds",
    )
    payment = result(
        reader.PAYMENT_FIELDS,
        [[8, 43, "completed", "2026-09-07T00:00:00Z", "2026-09-07T01:00:00Z"]],
        table="payments",
    )
    order = result(reader.ORDER_FIELDS, [[row[f] for f in reader.ORDER_FIELDS]])
    query = AsyncMock(side_effect=[refund, {**payment, "status": "pending"}, refund, payment, order])
    monkeypatch.setattr(reader, "_connector", AsyncMock(return_value=object()))
    monkeypatch.setattr(reader, "call_external_mcp_tool", query)
    progress, reserve = {"refund_after_id": 6}, AsyncMock(return_value=True)
    page = await read_with_recovery(
        lambda: reader.read_changed_refund_orders(
            AsyncMock(), uuid4(), BINDING, NOW.replace(day=7, hour=0), NOW, after_id=6, now=NOW
        ),
        retry_calls=18,
        progress=progress,
        reserve=reserve,
        save=AsyncMock(),
        remaining=lambda: 60,
        sleep=AsyncMock(),
        stage="source_refund_page",
    )
    reserve.assert_awaited_once_with(18)
    assert query.await_count == 5
    assert query.call_args_list[0].args[2] == query.call_args_list[2].args[2]
    assert progress["refund_after_id"] == 6
    assert page["refunds"][0]["order_reference"] == row["number"]
    assert page["refunds"][0]["amount"] == "100.01"
    assert progress["last_read_failure"]["resolved"] is True
