"""TDD: Extract distinct values from SuiteQL results for follow-up queries.

When a SuiteQL query returns data, extract distinct values from categorical
columns and append them to the result. This prevents the LLM from building
IN(...) lists from memory (which drops variants like "Lotus - Refurbished").
"""

import json

import pytest

from app.services.chat.tool_call_results import extract_distinct_values


class TestExtractDistinctValues:
    def test_extracts_string_columns(self):
        """Should extract distinct values from string columns."""
        result = {
            "columns": ["platform", "week", "qty"],
            "rows": [
                ["Azalea", "2026-W01", "100"],
                ["Lotus", "2026-W01", "50"],
                ["Lotus - Refurbished", "2026-W01", "5"],
                ["Azalea", "2026-W02", "120"],
                ["Lotus", "2026-W02", "60"],
            ],
            "row_count": 5,
        }

        values = extract_distinct_values(result)

        assert "platform" in values
        assert set(values["platform"]) == {"Azalea", "Lotus", "Lotus - Refurbished"}

    def test_skips_numeric_columns(self):
        """Columns with all numeric values should be skipped."""
        result = {
            "columns": ["id", "name", "qty"],
            "rows": [
                ["1", "Widget", "100"],
                ["2", "Gadget", "200"],
            ],
            "row_count": 2,
        }

        values = extract_distinct_values(result)

        assert "id" not in values
        assert "qty" not in values
        assert "name" in values

    def test_skips_high_cardinality_columns(self):
        """Columns with > 30 distinct values should be skipped (not categorical)."""
        result = {
            "columns": ["tranid", "customer"],
            "rows": [[f"SO-{i}", f"Customer {i}"] for i in range(40)],
            "row_count": 40,
        }

        values = extract_distinct_values(result)

        # Both have 40 distinct values — skip
        assert "tranid" not in values
        assert "customer" not in values

    def test_skips_date_columns(self):
        """Date-like columns should be skipped."""
        result = {
            "columns": ["trandate", "status", "week_start"],
            "rows": [
                ["2026-01-01", "Active", "2026-W01"],
                ["2026-01-02", "Closed", "2026-W01"],
            ],
            "row_count": 2,
        }

        values = extract_distinct_values(result)

        assert "trandate" not in values
        assert "week_start" not in values
        assert "status" in values

    def test_empty_result_returns_empty(self):
        """Empty results should return empty dict."""
        result = {"columns": ["a"], "rows": [], "row_count": 0}

        values = extract_distinct_values(result)

        assert values == {}

    def test_non_dict_returns_empty(self):
        """Non-dict input should return empty dict."""
        assert extract_distinct_values("not json") == {}
        assert extract_distinct_values(None) == {}

    def test_preserves_exact_values(self):
        """Values must be preserved exactly — no trimming, no dedup of variants."""
        result = {
            "columns": ["platform"],
            "rows": [
                ["Lotus"],
                ["Lotus - Refurbished"],
                ["Tiger Lake"],
                ["Tiger Lake - Refurbished"],
            ],
            "row_count": 4,
        }

        values = extract_distinct_values(result)

        assert len(values["platform"]) == 4
        assert "Lotus - Refurbished" in values["platform"]
        assert "Tiger Lake - Refurbished" in values["platform"]


class TestAppendDistinctValues:
    def test_appends_to_result_string(self):
        """Should append distinct values section to the result JSON."""
        from app.services.chat.tool_call_results import append_distinct_values

        result_str = json.dumps({
            "columns": ["platform", "qty"],
            "rows": [["Azalea", "10"], ["Lotus", "20"], ["Lotus - Refurbished", "5"]],
            "row_count": 3,
        })

        enriched = append_distinct_values(result_str)
        parsed = json.loads(enriched)

        assert "_distinct_values" in parsed
        assert "Lotus - Refurbished" in parsed["_distinct_values"]["platform"]

    def test_no_op_for_small_results(self):
        """Results with < 2 rows shouldn't get distinct values (not useful)."""
        from app.services.chat.tool_call_results import append_distinct_values

        result_str = json.dumps({
            "columns": ["name"],
            "rows": [["one"]],
            "row_count": 1,
        })

        enriched = append_distinct_values(result_str)

        assert enriched == result_str  # Unchanged

    def test_no_op_for_non_json(self):
        """Non-JSON strings should pass through unchanged."""
        from app.services.chat.tool_call_results import append_distinct_values

        assert append_distinct_values("error text") == "error text"
