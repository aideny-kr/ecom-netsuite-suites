"""Tests for the result cache."""

from unittest.mock import patch

import pytest

from app.services.chat.result_cache import (
    MAX_RESULTS_PER_CONVERSATION,
    CachedResult,
    cache_result,
    get_latest_result,
    get_result_by_message,
)


@pytest.fixture
def mock_redis():
    """Mock Redis with an in-memory dict."""
    store = {}

    class FakeRedis:
        def hset(self, key, field, value):
            store.setdefault(key, {})[field] = value

        def hget(self, key, field):
            return store.get(key, {}).get(field)

        def hgetall(self, key):
            return store.get(key, {})

        def hdel(self, key, field):
            store.get(key, {}).pop(field, None)

        def expire(self, key, ttl):
            pass

    with patch("app.services.chat.result_cache._get_redis", return_value=FakeRedis()):
        yield store


class TestCachedResultSerialization:
    def test_roundtrip(self):
        cr = CachedResult(
            message_id="msg-1",
            conversation_id="conv-1",
            result_type="suiteql",
            columns=["a", "b"],
            rows=[[1, 2]],
            row_count=1,
            query_text="SELECT 1",
        )
        restored = CachedResult.from_json(cr.to_json())
        assert restored.message_id == "msg-1"
        assert restored.columns == ["a", "b"]
        assert restored.rows == [[1, 2]]


class TestResultCache:
    @pytest.mark.asyncio
    async def test_cache_and_retrieve(self, mock_redis):
        cr = CachedResult(
            message_id="msg-1",
            conversation_id="conv-1",
            result_type="suiteql",
            columns=["name", "amount"],
            rows=[["Widget", 100]],
            row_count=1,
        )
        await cache_result("conv-1", "msg-1", cr)
        result = await get_latest_result("conv-1")
        assert result is not None
        assert result.message_id == "msg-1"
        assert result.columns == ["name", "amount"]

    @pytest.mark.asyncio
    async def test_get_by_message_id(self, mock_redis):
        cr = CachedResult(
            message_id="msg-2",
            conversation_id="conv-1",
            result_type="bigquery",
            columns=["date", "revenue"],
            rows=[["2026-01", 50000]],
            row_count=1,
        )
        await cache_result("conv-1", "msg-2", cr)
        result = await get_result_by_message("conv-1", "msg-2")
        assert result is not None
        assert result.result_type == "bigquery"

    @pytest.mark.asyncio
    async def test_latest_returns_most_recent(self, mock_redis):
        import time

        for i in range(3):
            cr = CachedResult(
                message_id=f"msg-{i}",
                conversation_id="conv-1",
                result_type="suiteql",
                columns=["col"],
                rows=[[i]],
                row_count=1,
                created_at=time.time() + i,
            )
            await cache_result("conv-1", f"msg-{i}", cr)
        result = await get_latest_result("conv-1")
        assert result.message_id == "msg-2"

    @pytest.mark.asyncio
    async def test_eviction_removes_oldest(self, mock_redis):
        import time

        for i in range(MAX_RESULTS_PER_CONVERSATION + 1):
            cr = CachedResult(
                message_id=f"msg-{i}",
                conversation_id="conv-1",
                result_type="suiteql",
                columns=["col"],
                rows=[[i]],
                row_count=1,
                created_at=time.time() + i,
            )
            await cache_result("conv-1", f"msg-{i}", cr)
        result = await get_result_by_message("conv-1", "msg-0")
        assert result is None
        result = await get_result_by_message("conv-1", f"msg-{MAX_RESULTS_PER_CONVERSATION}")
        assert result is not None

    @pytest.mark.asyncio
    async def test_no_redis_returns_none(self):
        with patch("app.services.chat.result_cache._get_redis", return_value=None):
            result = await get_latest_result("conv-1")
            assert result is None

    @pytest.mark.asyncio
    async def test_empty_conversation_returns_none(self, mock_redis):
        result = await get_latest_result("conv-nonexistent")
        assert result is None
