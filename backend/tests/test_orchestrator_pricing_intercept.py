"""Step 5 — orchestrator interceptor: pricing_state pipes into cache payload.

Covers the changes to ``_intercept_tool_result`` and ``_on_tool_intercepted``
that route pricing tool results through the typed cache payload so
``pricing_revise`` and ``pricing_to_sheets`` can read prior state.
"""

from __future__ import annotations

import inspect
import json

import pytest

from app.services.chat.orchestrator import _intercept_tool_result


def _pricing_result_str(*, success: bool = True, with_state: bool = True) -> str:
    body: dict = {
        "success": success,
        "sku_count": 2,
        "currency_count": 3,
        "output_files": {"excel": "file-1", "netsuite_csv": "file-2"},
        "preview": [{"SKU": "X", "USD": 100.0, "GBP": 99.0}],
        "template_mode": False,
    }
    if with_state:
        body["pricing_state"] = {
            "seed_items": [{"sku": "X", "usd_price": "100", "item_name": None}],
            "effective_items": [{"sku": "X", "usd_price": "100", "item_name": None}],
            "effective_currencies": ["GBP", "EUR"],
            "effective_fx_overrides": {},
            "effective_vat_overrides": {},
            "effective_rounding_overrides": {},
            "effective_uplift_by_currency": {},
            "applied_overrides_log": [],
            "excel_file_id": "file-1",
            "netsuite_csv_file_id": "file-2",
            "header_columns": ["SKU", "Item Name", "USD", "EUR", "GBP"],
            "row_count": 2,
        }
    return json.dumps(body)


class TestPricingInterceptPipesPricingState:
    def test_pricing_convert_event_includes_pricing_state(self):
        event_type, event_data, _ = _intercept_tool_result(
            "pricing_convert", _pricing_result_str()
        )
        assert event_type == "task_output"
        assert "pricing_state" in event_data
        assert event_data["pricing_state"]["excel_file_id"] == "file-1"
        assert event_data["pricing_state"]["row_count"] == 2

    def test_pricing_export_event_includes_pricing_state(self):
        event_type, event_data, _ = _intercept_tool_result(
            "pricing_export", _pricing_result_str()
        )
        assert event_type == "task_output"
        assert "pricing_state" in event_data

    def test_pricing_revise_event_includes_pricing_state(self):
        event_type, event_data, _ = _intercept_tool_result(
            "pricing_revise", _pricing_result_str()
        )
        assert event_type == "task_output"
        assert "pricing_state" in event_data

    def test_pricing_revise_dotted_alias(self):
        event_type, event_data, _ = _intercept_tool_result(
            "pricing.revise", _pricing_result_str()
        )
        assert event_type == "task_output"
        assert "pricing_state" in event_data

    def test_pricing_state_absent_when_executor_omits_it(self):
        """Backward-compat: if pricing_state is missing the interceptor still
        emits a task_output event but pricing_state is absent (or None)."""
        event_type, event_data, _ = _intercept_tool_result(
            "pricing_convert", _pricing_result_str(with_state=False)
        )
        assert event_type == "task_output"
        assert event_data.get("pricing_state") is None


class TestPricingToSheetsEmitsSheetsLink:
    def test_emits_sheets_link_event(self):
        result_str = json.dumps(
            {
                "success": True,
                "spreadsheet_id": "ss-123",
                "url": "https://docs.google.com/spreadsheets/d/ss-123/edit",
                "title": "Pricing Export — 2026-04-30",
                "sku_count": 50,
            }
        )
        event_type, event_data, _ = _intercept_tool_result(
            "pricing_to_sheets", result_str
        )
        assert event_type == "sheets_link"
        assert event_data["url"].startswith("https://docs.google.com/")
        assert event_data["spreadsheet_id"] == "ss-123"

    def test_dotted_alias(self):
        result_str = json.dumps(
            {
                "success": True,
                "spreadsheet_id": "ss-123",
                "url": "https://docs.google.com/spreadsheets/d/ss-123/edit",
                "title": "T",
            }
        )
        event_type, _, _ = _intercept_tool_result("pricing.to_sheets", result_str)
        assert event_type == "sheets_link"

    def test_error_passes_through(self):
        result_str = json.dumps({"error": True, "message": "no connector"})
        event_type, event_data, _ = _intercept_tool_result(
            "pricing_to_sheets", result_str
        )
        assert event_type is None
        assert event_data is None


class TestPricingStateStrippedFromSSE:
    """Codex review finding: ``pricing_state`` carries the full seed_items /
    effective_items list (~150KB on a 5K-SKU catalog). The frontend only
    needs the preview to render the task_output card. Yielding pricing_state
    over SSE inflates the payload AND ends up persisted in chat
    structured_output. The cache callback gets the full payload; SSE gets
    a stripped copy.
    """

    def test_make_tool_interceptor_strips_pricing_state_from_sse(self):
        from app.services.chat.orchestrator import _make_tool_interceptor

        captured: dict = {}

        def _cache_cb(tool_name, event_type_str, event_data):
            # Cache callback must see the FULL payload (pricing_state included).
            captured["cache_event_data"] = dict(event_data)

        interceptor = _make_tool_interceptor(cache_callback=_cache_cb)

        result_str = json.dumps(
            {
                "success": True,
                "sku_count": 2,
                "currency_count": 3,
                "output_files": {"excel": "f1", "netsuite_csv": "f2"},
                "preview": [{"SKU": "X", "USD": 100.0, "GBP": 99.0}],
                "template_mode": False,
                "pricing_state": {
                    "seed_items": [{"sku": "X", "usd_price": "100", "item_name": None}],
                    "effective_items": [{"sku": "X", "usd_price": "100", "item_name": None}],
                    "excel_file_id": "f1",
                    "row_count": 2,
                },
            }
        )
        sse_tuple, _ = interceptor("pricing_revise", result_str)
        assert sse_tuple is not None
        sse_event_type, sse_event_data = sse_tuple
        # Cache callback got pricing_state.
        assert "pricing_state" in captured["cache_event_data"]
        # SSE event data did NOT.
        assert "pricing_state" not in sse_event_data
        # Frontend-needed fields preserved.
        assert sse_event_data["preview"] == [{"SKU": "X", "USD": 100.0, "GBP": 99.0}]
        assert sse_event_data["sku_count"] == 2


class TestSheetsLinkDoesNotPolluteCache:
    """Codex review finding: if pricing_to_sheets fires within a turn that
    also did a pricing_export / pricing_revise, the sheets_link event must
    NOT generate a junk 'suiteql' cache entry that overwrites the pricing
    payload at flush time."""

    def test_sheets_link_event_does_not_create_cache_entry(self):
        """Static check: the cache callback must skip non-data SSE events."""
        import inspect

        from app.services.chat import orchestrator

        source = inspect.getsource(orchestrator)
        # _on_tool_intercepted must explicitly filter out sheets_link / docs_link.
        # Either by name listing, or by an early-return.
        assert "sheets_link" in source and "docs_link" in source, (
            "_on_tool_intercepted must reference sheets_link and docs_link to skip them"
        )

    def test_pricing_to_sheets_in_skipped_event_set(self):
        """Verify the cache-skip logic exists for pricing_to_sheets's SSE event."""
        import inspect

        from app.services.chat import orchestrator

        source = inspect.getsource(orchestrator)
        # Locate _on_tool_intercepted and verify it has an early-return / skip
        # branch for non-data events.
        idx = source.index("_on_tool_intercepted")
        body_window = source[idx : idx + 2500]
        # The skip logic must reference "sheets_link" or use _NON_DATA_EVENTS.
        assert ("sheets_link" in body_window) or ("_NON_DATA_EVENTS" in body_window), (
            "_on_tool_intercepted must skip sheets_link / docs_link events"
        )


class TestSameTurnPricingStateRead:
    """Codex review finding: if the user asks 'generate pricing AND export to
    sheets' in one turn, pricing_to_sheets's call to get_latest_result_by_type
    must see the pricing entry just written by pricing_convert / pricing_export
    earlier in the same turn. The cache MUST be flushed eagerly, not deferred
    to after the agent loop completes."""

    def test_immediate_cache_write_in_intercept(self):
        """Static check: the intercept callback must write to the cache
        immediately (synchronously / via a sync helper), not just queue."""
        import inspect

        from app.services.chat import orchestrator

        source = inspect.getsource(orchestrator)
        idx = source.index("_on_tool_intercepted")
        body_window = source[idx : idx + 2500]
        # The callback must reference a sync cache write — either
        # _cache_result_sync or an immediate call to cache_result via
        # asyncio.create_task.
        assert (
            "_cache_result_sync" in body_window
            or "asyncio.create_task(cache_result" in body_window
            or "ensure_future(cache_result" in body_window
        ), (
            "_on_tool_intercepted must write to the cache eagerly so same-turn "
            "pricing follow-ups (pricing_export → pricing_to_sheets in one turn) "
            "can read the just-written entry."
        )


class TestOnToolInterceptedWiring:
    """Static checks against the orchestrator closure that builds CachedResult.

    Verifies the source contains the required wiring without needing to spin
    up the full run_chat_turn pipeline.
    """

    def test_orchestrator_defines_pricing_write_tools_set(self):
        from app.services.chat import orchestrator

        source = inspect.getsource(orchestrator)
        assert "_PRICING_WRITE_TOOLS" in source
        # Set membership for all three pricing-write tools.
        assert '"pricing_convert"' in source
        assert '"pricing_export"' in source
        assert '"pricing_revise"' in source

    def test_on_tool_intercepted_pipes_pricing_state_to_payload(self):
        from app.services.chat import orchestrator

        source = inspect.getsource(orchestrator)
        # Verify the cache callback consults _PRICING_WRITE_TOOLS for routing.
        assert "_PRICING_WRITE_TOOLS" in source
        # Verify it reads pricing_state from event_data and assigns to payload.
        assert 'event_data.get("pricing_state")' in source

    def test_pricing_to_sheets_NOT_in_write_tools(self):
        """pricing_to_sheets is read-only — it must NOT appear in the write set."""
        from app.services.chat import orchestrator

        source = inspect.getsource(orchestrator)
        # Locate the _PRICING_WRITE_TOOLS definition and assert pricing_to_sheets
        # does not appear *inside* that set literal.
        idx = source.index("_PRICING_WRITE_TOOLS")
        # Slice ~200 chars after the constant name — should cover the set literal.
        window = source[idx : idx + 200]
        assert "pricing_to_sheets" not in window, (
            "pricing_to_sheets must not be in _PRICING_WRITE_TOOLS — it's a read-only consumer"
        )
