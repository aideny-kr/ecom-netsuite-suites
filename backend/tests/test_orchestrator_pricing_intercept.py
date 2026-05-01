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
