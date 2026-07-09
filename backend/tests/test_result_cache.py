"""Tests for the result cache."""

import json
from unittest.mock import patch

import pytest

from app.services.chat.result_cache import (
    MAX_RESULTS_PER_CONVERSATION,
    CachedResult,
    _cache_result_sync,
    _full_payload_key,
    cache_full_payload,
    cache_result,
    get_full_payload,
    get_full_payload_entry,
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

        def hlen(self, key):
            return len(store.get(key, {}))

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

    def test_sync_helper_writes_immediately(self, mock_redis):
        """_cache_result_sync must write to Redis synchronously so the
        orchestrator's intercept callback can read its own writes back via
        get_latest_result_by_type within the same turn (e.g.,
        pricing_export → pricing_to_sheets in one assistant message)."""
        import asyncio

        cr = CachedResult(
            message_id="pending-abc123",
            conversation_id="conv-1",
            result_type="pricing",
            columns=[],
            rows=[],
            row_count=5,
            payload={"excel_file_id": "file-xyz"},
        )
        # No await — pure sync write.
        _cache_result_sync("conv-1", "pending-abc123", cr)
        # Async helper sees the entry.
        result = asyncio.run(get_latest_result_by_type("conv-1", "pricing"))
        assert result is not None
        assert result.payload == {"excel_file_id": "file-xyz"}

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


class TestFullPayloadSidecar:
    """The eager, in-turn FULL-PAYLOAD sidecar (gate cluster A).

    ``report.compose`` must resolve the results just computed THIS turn — but the
    current turn's assistant ChatMessage is not persisted until AFTER the agent
    loop. The sidecar writes the FULL, uncapped result_payload to Redis under a
    deterministic turn-scoped ``result_id`` (r1, r2, ...) the instant a data tool
    is intercepted, so a same-turn compose can read it.
    """

    def test_full_payload_roundtrip_uncapped(self, mock_redis):
        """A >50-row payload survives uncapped — the sidecar must NOT apply the
        50-row preview truncation that CachedResult.to_json does."""
        big_rows = [[f"P{i}", str(i)] for i in range(120)]
        payload = {
            "kind": "table",
            "columns": ["Period", "N"],
            "rows": big_rows,
            "row_count": 120,
        }
        cache_full_payload("conv-1", "r1", payload)
        got = get_full_payload("conv-1", "r1")
        assert got is not None
        assert got["row_count"] == 120
        assert len(got["rows"]) == 120  # uncapped — not 50

    def test_full_payload_miss_returns_none(self, mock_redis):
        assert get_full_payload("conv-1", "r99") is None
        assert get_full_payload("no-such-conv", "r1") is None

    def test_full_payload_no_redis_is_safe(self):
        with patch("app.services.chat.result_cache._get_redis", return_value=None):
            cache_full_payload("conv-1", "r1", {"rows": []})  # no raise
            assert get_full_payload("conv-1", "r1") is None

    def test_full_payload_evicts_oldest_over_cap(self, mock_redis):
        """The sidecar caps the LIST per conversation at
        MAX_FULL_PAYLOADS_PER_CONVERSATION, evicting the OLDEST result_id first
        (FIFO by write order)."""
        from app.services.chat.result_cache import MAX_FULL_PAYLOADS_PER_CONVERSATION

        n = MAX_FULL_PAYLOADS_PER_CONVERSATION
        for i in range(1, n + 2):  # write one past the cap
            cache_full_payload("conv-1", f"r{i}", {"rows": [[i]], "row_count": 1})
        # r1 (oldest) is evicted; r2..r{n+1} survive.
        assert get_full_payload("conv-1", "r1") is None
        for i in range(2, n + 2):
            assert get_full_payload("conv-1", f"r{i}") is not None

    def test_full_payload_cap_covers_a_parallel_heavy_turn(self, mock_redis):
        """Live-QA regression (2026-07-09) + gate r1 #0: the sidecar cap was 6
        (borrowed from the PREVIEW cache) and FIFO-evicted r1 of a 7-data-call turn
        MID-TURN — 'Data unavailable' + fail-closed recipe capture. And
        CHAT_MAX_TOOL_CALLS_PER_TURN caps LLM STEPS, not tool calls (one step can
        carry several parallel tool_use blocks), so the backstop carries 4x headroom
        over the step cap. It is a runaway backstop, not an invariant — past it, the
        loud compose refusal makes the miss actionable instead of silent."""
        from app.core.config import settings

        n = 2 * settings.CHAT_MAX_TOOL_CALLS_PER_TURN  # a parallel-heavy turn: 2 blocks/step
        for i in range(1, n + 1):
            cache_full_payload("conv-1", f"r{i}", {"rows": [[i]], "row_count": 1})
        assert get_full_payload("conv-1", "r1") is not None, (
            "the earliest result of a parallel-heavy turn must survive to compose time"
        )

    def test_full_payload_is_conversation_scoped(self, mock_redis):
        """The same result_id in two conversations is isolated."""
        cache_full_payload("conv-A", "r1", {"rows": [["a"]], "row_count": 1})
        cache_full_payload("conv-B", "r1", {"rows": [["b"]], "row_count": 1})
        assert get_full_payload("conv-A", "r1")["rows"] == [["a"]]
        assert get_full_payload("conv-B", "r1")["rows"] == [["b"]]

    def test_conversation_ordinal_ids_do_not_overwrite_across_turns(self, mock_redis):
        """re-gate r2 (findings #5/#9/#13): the sidecar is conversation-scoped (no
        turn component in the key), so with PER-TURN ids turn B's r1 would overwrite
        turn A's r1. Conversation-ORDINAL ids (turn A → r1, turn B → r2) keep BOTH
        turns' payloads resolvable — the cross-turn collision the fix eliminates."""
        # Turn A's single result is r1.
        cache_full_payload("conv-1", "r1", {"rows": [["turnA"]], "row_count": 1})
        # Turn B's first result is r2 (conversation-ordinal), NOT r1 — so it does
        # not clobber turn A's payload.
        cache_full_payload("conv-1", "r2", {"rows": [["turnB"]], "row_count": 1})
        assert get_full_payload("conv-1", "r1")["rows"] == [["turnA"]], (
            "turn A's r1 payload must survive turn B (no overwrite)"
        )
        assert get_full_payload("conv-1", "r2")["rows"] == [["turnB"]]

    # --- Recipe meta (Slice A of live-dashboard reports) ------------------------------
    # The interceptor callback receives {tool_name, params} and used to discard them;
    # the envelope now carries them so a same-turn report.compose can capture the
    # refresh recipe's per-result_id {tool, params} without re-deriving anything.

    def test_entry_carries_tool_and_params(self, mock_redis):
        cache_full_payload(
            "conv-1",
            "r1",
            {"rows": [[1]], "row_count": 1},
            tool_name="netsuite_suiteql",
            params={"query": "SELECT 1 FROM transaction"},
        )
        entry = get_full_payload_entry("conv-1", "r1")
        assert entry is not None
        assert entry["tool"] == "netsuite_suiteql"
        assert entry["params"] == {"query": "SELECT 1 FROM transaction"}
        assert entry["payload"]["row_count"] == 1
        # the payload-only reader is unchanged by the meta
        assert get_full_payload("conv-1", "r1")["rows"] == [[1]]

    def test_old_envelope_without_meta_still_resolves_payload(self, mock_redis):
        """A pre-deploy envelope ({payload, seq} only — e.g. written mid-rollover)
        must stay readable: get_full_payload unchanged; the entry reader returns it
        WITHOUT a tool key (recipe capture then falls to the persisted fallback)."""
        old_envelope = json.dumps({"payload": {"rows": [["old"]], "row_count": 1}, "seq": 1.0})
        mock_redis.setdefault(_full_payload_key("conv-1"), {})["r1"] = old_envelope
        assert get_full_payload("conv-1", "r1")["rows"] == [["old"]]
        entry = get_full_payload_entry("conv-1", "r1")
        assert entry is not None and "tool" not in entry

    def test_entry_miss_and_no_redis_are_safe(self):
        with patch("app.services.chat.result_cache._get_redis", return_value=None):
            assert get_full_payload_entry("conv-1", "r1") is None

    def test_meta_write_does_not_break_fifo_eviction(self, mock_redis):
        from app.services.chat.result_cache import MAX_FULL_PAYLOADS_PER_CONVERSATION

        n = MAX_FULL_PAYLOADS_PER_CONVERSATION
        for i in range(1, n + 2):  # one past the cap, every write meta-bearing
            cache_full_payload(
                "conv-1", f"r{i}", {"rows": [[i]], "row_count": 1}, tool_name="netsuite_suiteql", params={"q": i}
            )
        assert get_full_payload("conv-1", "r1") is None  # oldest evicted
        assert get_full_payload_entry("conv-1", f"r{n + 1}")["tool"] == "netsuite_suiteql"
