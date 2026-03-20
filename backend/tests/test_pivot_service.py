"""TDD: Server-side pivot tool — deterministic pivoting from SuiteQL results.

The LLM cannot reliably build CASE WHEN pivot SQL. This tool takes a flat
GROUP BY result and pivots it in Python. Only values that exist in the data
become columns — no hallucinated values, no dropped variants.
"""

import pytest

from app.services.pivot_service import pivot_rows


class TestPivotBasic:
    def test_simple_pivot(self):
        """3 platforms × 2 weeks → 2 rows, 3 platform columns + Total."""
        columns = ["week", "platform", "qty"]
        rows = [
            ["W01", "Azalea", "100"],
            ["W01", "Lotus", "50"],
            ["W01", "Tulip", "30"],
            ["W02", "Azalea", "120"],
            ["W02", "Lotus", "60"],
            ["W02", "Tulip", "40"],
        ]

        out_cols, out_rows = pivot_rows(
            columns=columns, rows=rows,
            row_field="week", column_field="platform", value_field="qty",
        )

        assert out_cols == ["week", "Azalea", "Lotus", "Tulip", "Total"]
        assert len(out_rows) == 2
        assert out_rows[0] == ["W01", 100.0, 50.0, 30.0, 180.0]
        assert out_rows[1] == ["W02", 120.0, 60.0, 40.0, 220.0]


class TestRefurbishedVariants:
    def test_refurbished_preserved_as_separate_columns(self):
        """'Lotus' and 'Lotus - Refurbished' must be separate columns."""
        columns = ["week", "platform", "qty"]
        rows = [
            ["W01", "Lotus", "50"],
            ["W01", "Lotus - Refurbished", "5"],
            ["W01", "Azalea", "100"],
        ]

        out_cols, out_rows = pivot_rows(
            columns=columns, rows=rows,
            row_field="week", column_field="platform", value_field="qty",
        )

        assert "Lotus" in out_cols
        assert "Lotus - Refurbished" in out_cols
        assert out_cols.index("Lotus") != out_cols.index("Lotus - Refurbished")


class TestZeroDataExcluded:
    def test_no_phantom_columns(self):
        """Only platforms present in data become columns — no Bamboo if no Bamboo rows."""
        columns = ["week", "platform", "qty"]
        rows = [
            ["W01", "Azalea", "100"],
            ["W01", "Dogwood", "50"],
        ]

        out_cols, out_rows = pivot_rows(
            columns=columns, rows=rows,
            row_field="week", column_field="platform", value_field="qty",
        )

        assert "Bamboo" not in out_cols
        assert "Azalea" in out_cols
        assert "Dogwood" in out_cols


class TestAggregation:
    def test_sum_aggregation(self):
        """Multiple rows for same row+column → summed."""
        columns = ["week", "platform", "qty"]
        rows = [
            ["W01", "Azalea", "60"],
            ["W01", "Azalea", "40"],  # Same week+platform
        ]

        out_cols, out_rows = pivot_rows(
            columns=columns, rows=rows,
            row_field="week", column_field="platform", value_field="qty",
            aggregation="sum",
        )

        assert out_rows[0][1] == 100.0  # 60 + 40

    def test_count_aggregation(self):
        columns = ["week", "platform", "qty"]
        rows = [
            ["W01", "Azalea", "60"],
            ["W01", "Azalea", "40"],
        ]

        out_cols, out_rows = pivot_rows(
            columns=columns, rows=rows,
            row_field="week", column_field="platform", value_field="qty",
            aggregation="count",
        )

        assert out_rows[0][1] == 2  # 2 rows


class TestNoTotal:
    def test_exclude_total_column(self):
        columns = ["week", "platform", "qty"]
        rows = [["W01", "Azalea", "100"]]

        out_cols, out_rows = pivot_rows(
            columns=columns, rows=rows,
            row_field="week", column_field="platform", value_field="qty",
            include_total=False,
        )

        assert "Total" not in out_cols


class TestEdgeCases:
    def test_empty_rows(self):
        columns = ["week", "platform", "qty"]
        rows = []

        out_cols, out_rows = pivot_rows(
            columns=columns, rows=rows,
            row_field="week", column_field="platform", value_field="qty",
        )

        assert out_cols == ["week"]
        assert out_rows == []

    def test_missing_field_raises(self):
        columns = ["week", "platform", "qty"]
        rows = [["W01", "Azalea", "100"]]

        with pytest.raises(ValueError, match="not found"):
            pivot_rows(
                columns=columns, rows=rows,
                row_field="week", column_field="NONEXISTENT", value_field="qty",
            )

    def test_null_values_treated_as_zero(self):
        columns = ["week", "platform", "qty"]
        rows = [
            ["W01", "Azalea", None],
            ["W01", "Lotus", "50"],
        ]

        out_cols, out_rows = pivot_rows(
            columns=columns, rows=rows,
            row_field="week", column_field="platform", value_field="qty",
        )

        azalea_idx = out_cols.index("Azalea")
        assert out_rows[0][azalea_idx] == 0.0

    def test_row_order_preserved(self):
        """Row order should match first appearance in data."""
        columns = ["week", "platform", "qty"]
        rows = [
            ["W03", "Azalea", "10"],
            ["W01", "Azalea", "30"],
            ["W02", "Azalea", "20"],
        ]

        out_cols, out_rows = pivot_rows(
            columns=columns, rows=rows,
            row_field="week", column_field="platform", value_field="qty",
        )

        assert [r[0] for r in out_rows] == ["W03", "W01", "W02"]
