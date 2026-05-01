"""Tests for the result cache."""

import json
from unittest.mock import patch

import pytest

from app.services.chat.result_cache import (
    MAX_RESULTS_PER_CONVERSATION,
    CachedResult,
    cache_result,
    get_latest_result,
    get_latest_result_by_type,
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


class TestPayloadField:
    def test_payload_field_round_trips(self):
        cr = CachedResult(
            message_id="msg-1",
            conversation_id="conv-1",
            result_type="pricing",
            columns=[],
            rows=[],
            row_count=10,
            payload={"excel_file_id": "file-abc", "row_count": 10, "headers": ["SKU", "USD", "GBP"]},
        )
        restored = CachedResult.from_json(cr.to_json())
        assert restored.payload == {"excel_file_id": "file-abc", "row_count": 10, "headers": ["SKU", "USD", "GBP"]}

    def test_payload_field_optional_old_entries_deserialize(self):
        # Pre-payload JSON shape (no payload field) must round-trip without error.
        old_shape = json.dumps(
            {
                "message_id": "msg-old",
                "conversation_id": "conv-1",
                "result_type": "suiteql",
                "columns": ["a"],
                "rows": [[1]],
                "row_count": 1,
                "summary": None,
                "query_text": "SELECT 1",
                "created_at": 100.0,
            }
        )
        restored = CachedResult.from_json(old_shape)
        assert restored.message_id == "msg-old"
        assert restored.payload is None

    def test_payload_field_default_none(self):
        cr = CachedResult(
            message_id="msg-1",
            conversation_id="conv-1",
            result_type="suiteql",
            columns=["a"],
            rows=[[1]],
            row_count=1,
        )
        assert cr.payload is None


class TestGetLatestResultByType:
    @pytest.mark.asyncio
    async def test_filters_correctly(self, mock_redis):
        import time

        # SuiteQL written first
        await cache_result(
            "conv-1",
            "msg-suiteql",
            CachedResult(
                message_id="msg-suiteql",
                conversation_id="conv-1",
                result_type="suiteql",
                columns=["a"],
                rows=[[1]],
                row_count=1,
                created_at=time.time() + 1,
            ),
        )
        # Pricing written next
        await cache_result(
            "conv-1",
            "msg-pricing",
            CachedResult(
                message_id="msg-pricing",
                conversation_id="conv-1",
                result_type="pricing",
                columns=[],
                rows=[],
                row_count=5,
                payload={"excel_file_id": "file-xyz"},
                created_at=time.time() + 2,
            ),
        )
        # BigQuery is the most recent overall
        await cache_result(
            "conv-1",
            "msg-bq",
            CachedResult(
                message_id="msg-bq",
                conversation_id="conv-1",
                result_type="bigquery",
                columns=["b"],
                rows=[[2]],
                row_count=1,
                created_at=time.time() + 3,
            ),
        )

        # Untyped helper picks the absolute latest (bq).
        latest_any = await get_latest_result("conv-1")
        assert latest_any.result_type == "bigquery"

        # Typed helper picks the latest pricing entry, even though bq is newer.
        latest_pricing = await get_latest_result_by_type("conv-1", "pricing")
        assert latest_pricing is not None
        assert latest_pricing.message_id == "msg-pricing"
        assert latest_pricing.payload == {"excel_file_id": "file-xyz"}

    @pytest.mark.asyncio
    async def test_returns_none_when_type_absent(self, mock_redis):
        await cache_result(
            "conv-1",
            "msg-1",
            CachedResult(
                message_id="msg-1",
                conversation_id="conv-1",
                result_type="suiteql",
                columns=["a"],
                rows=[[1]],
                row_count=1,
            ),
        )
        result = await get_latest_result_by_type("conv-1", "pricing")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_redis_returns_none(self):
        with patch("app.services.chat.result_cache._get_redis", return_value=None):
            result = await get_latest_result_by_type("conv-1", "pricing")
            assert result is None


class TestEvictionPinsLatestPerType:
    @pytest.mark.asyncio
    async def test_pin_per_type_survives_eviction(self, mock_redis):
        import time

        # MAX is 6 in the new policy; fill 6 entries: 3 suiteql + 1 pricing + 1 bigquery + 1 financial.
        assert MAX_RESULTS_PER_CONVERSATION == 6

        base = time.time()
        entries = [
            ("msg-suiteql-1", "suiteql", base + 1),
            ("msg-suiteql-2", "suiteql", base + 2),
            ("msg-pricing-1", "pricing", base + 3),
            ("msg-bq-1", "bigquery", base + 4),
            ("msg-financial-1", "financial_report", base + 5),
            ("msg-suiteql-3", "suiteql", base + 6),
        ]
        for mid, rtype, ts in entries:
            await cache_result(
                "conv-1",
                mid,
                CachedResult(
                    message_id=mid,
                    conversation_id="conv-1",
                    result_type=rtype,
                    columns=["c"],
                    rows=[[1]],
                    row_count=1,
                    created_at=ts,
                ),
            )

        # Add a 7th entry — must trigger eviction of the OLDEST UNPINNED entry.
        # Pinned (latest of each type): suiteql-3, pricing-1, bq-1, financial-1.
        # Unpinned: suiteql-1, suiteql-2.  Oldest unpinned is suiteql-1 → evicted.
        await cache_result(
            "conv-1",
            "msg-suiteql-4",
            CachedResult(
                message_id="msg-suiteql-4",
                conversation_id="conv-1",
                result_type="suiteql",
                columns=["c"],
                rows=[[1]],
                row_count=1,
                created_at=base + 7,
            ),
        )

        # The latest pricing/bq/financial must survive.
        assert (await get_result_by_message("conv-1", "msg-pricing-1")) is not None
        assert (await get_result_by_message("conv-1", "msg-bq-1")) is not None
        assert (await get_result_by_message("conv-1", "msg-financial-1")) is not None
        # Oldest suiteql entry is gone.
        assert (await get_result_by_message("conv-1", "msg-suiteql-1")) is None

    @pytest.mark.asyncio
    async def test_pricing_survives_long_mixed_session(self, mock_redis):
        """Regression: pricing entry must survive 6 unrelated turns + still be retrievable."""
        import time

        base = time.time()
        # Initial pricing run
        await cache_result(
            "conv-1",
            "msg-pricing-1",
            CachedResult(
                message_id="msg-pricing-1",
                conversation_id="conv-1",
                result_type="pricing",
                columns=[],
                rows=[],
                row_count=10,
                payload={"excel_file_id": "file-xyz"},
                created_at=base,
            ),
        )
        # Six unrelated mixed-tool turns after the pricing run.
        for i in range(1, 7):
            rtype = "suiteql" if i % 2 == 0 else "bigquery"
            await cache_result(
                "conv-1",
                f"msg-other-{i}",
                CachedResult(
                    message_id=f"msg-other-{i}",
                    conversation_id="conv-1",
                    result_type=rtype,
                    columns=["c"],
                    rows=[[i]],
                    row_count=1,
                    created_at=base + i,
                ),
            )
        # Pricing payload must still be retrievable via type-filter.
        latest_pricing = await get_latest_result_by_type("conv-1", "pricing")
        assert latest_pricing is not None
        assert latest_pricing.payload == {"excel_file_id": "file-xyz"}
