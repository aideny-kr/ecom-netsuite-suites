"""Phase 4 — meaningful chart selection (composition).

Symptoms fixed: (a) the "Cash Balance Trend" section charted ~36 account bars instead
of a LINE over the periods; (b) driver charts mixed section subtotals AND their own
detail lines side by side (double-counting) with a grand-total bar dwarfing the rest;
(c) junk chart titles ("Chart", "amount by account"). All deterministic + structural:
time-series detection off the x column's VALUE SHAPE (never column names), driver
selection off line_meta (never labels).
"""

from __future__ import annotations

from app.services.report.report_service import (
    _looks_time_series,
    _resolve_data_section,
    assemble_spec,
)


def _meta(is_summary: bool, level: int = 0) -> dict:
    return {"is_summary": is_summary, "level": level}


def _months_payload() -> dict:
    """A monthly cash-balance table (the archetypal trend result)."""
    return {
        "columns": ["period", "cash_balance"],
        "rows": [[f"2026-{m:02d}", 5_000_000 + m * 250_000] for m in range(1, 7)],
        "row_count": 6,
        "currency_columns": ["cash_balance"],
    }


def _statement_payload() -> dict:
    rows = [
        ["Net Income", 5_200_000],
        ["11000 - Accounts Receivable", -1_400_000],
        ["12000 - Inventory", -600_000],
        ["13000 - Intercompany Receivables", -2_900_000],
        ["Operating Activities", 8_100_000],
        ["14000 - Fixed Assets", -2_300_000],
        ["Investing Activities", -2_300_000],
        ["Net Change in Cash", 4_750_000],
        ["Cash at End of Period", 11_500_000],
    ]
    meta = [
        _meta(True, 1),
        _meta(False, 2),
        _meta(False, 2),
        _meta(False, 2),
        _meta(True, 0),
        _meta(False, 1),
        _meta(True, 0),
        _meta(True, 0),
        _meta(True, 0),
    ]
    return {
        "columns": ["account", "amount"],
        "rows": rows,
        "row_count": 180,
        "currency_columns": ["amount"],
        "line_meta": meta,
    }


# ---------------------------------------------------------------------------
# Time-series detection: value SHAPE of the x column, never column names.
# ---------------------------------------------------------------------------
def test_time_like_values_detected():
    assert _looks_time_series(["2026-01", "2026-02", "2026-03"])
    assert _looks_time_series(["2026-01-31", "2026-02-28"])
    assert _looks_time_series(["Jan 2026", "Feb 2026", "Mar 2026"])
    assert _looks_time_series(["Q1 2026", "Q2 2026"])
    assert _looks_time_series(["FY25", "FY26"])


def test_categorical_values_not_detected():
    assert not _looks_time_series(["11000 - Accounts Receivable", "12000 - Inventory"])
    assert not _looks_time_series(["US", "UK", "DE"])
    assert not _looks_time_series([])  # too few to call it a series
    # a numeric id/code column is NOT a time axis
    assert not _looks_time_series(["11000", "12000", "14000"])


# ---------------------------------------------------------------------------
# Auto-chart type by data shape: monthly trend → LINE; categorical → bar.
# ---------------------------------------------------------------------------
def _auto_chart_of(payload: dict, sections=None) -> dict | None:
    spec = assemble_spec("R", sections or [{"type": "table", "result_id": "r1"}], lambda rid: payload)
    return next((s for s in spec["sections"] if s["type"] == "chart"), None)


def test_monthly_table_auto_charts_as_line():
    chart = _auto_chart_of(_months_payload())
    assert chart is not None
    assert chart["chart_type"] == "line"
    assert "<polyline" in chart["svg"]  # a real line, not bars


def test_categorical_table_auto_charts_as_bar():
    payload = {
        "columns": ["country", "amount"],
        "rows": [["US", 100], ["UK", 200], ["DE", 300]],
        "row_count": 3,
        "currency_columns": ["amount"],
    }
    chart = _auto_chart_of(payload)
    assert chart is not None
    assert chart["chart_type"] == "bar"


# ---------------------------------------------------------------------------
# Statement driver chart: leaf details only (no subtotal/grand-total bar-soup).
# ---------------------------------------------------------------------------
def test_statement_auto_chart_is_leaf_drivers_not_summaries():
    payload = _statement_payload()
    spec = assemble_spec("CF", [{"type": "table", "result_id": "r1"}], lambda rid: payload)
    chart = next(s for s in spec["sections"] if s["type"] == "chart")
    svg = chart["svg"]
    # leaf drivers charted…
    assert "11000 - Accounts" in svg or "11000 - Account" in svg or "11000" in svg
    assert "Intercompany" in svg
    # …but NO subtotal / grand-total bars next to their own details (double-count)
    assert "Operating Activities" not in svg
    assert "Cash at End of Period" not in svg
    assert chart["chart_type"] == "bar"  # categorical drivers stay bars
    # descriptive title, not "amount by account"/"Chart"
    assert "driver" in svg.lower()
    # the internal hand-off never leaks into the frozen spec
    assert all("statement_drivers" not in s for s in spec["sections"])


def test_driver_chart_keeps_top_k_by_magnitude_in_source_order():
    # 20 leaf details, ascending |amount|; the driver set is the largest K, source order.
    rows, meta = [], []
    for i in range(20):
        rows.append([f"4{i:04d} - Detail {i}", (i + 1) * 100])
        meta.append(_meta(False, 1))
    rows.append(["Grand Total", 999_999_999])
    meta.append(_meta(True, 0))
    rows.append(["Second Total", 1])
    meta.append(_meta(True, 0))
    payload = {
        "columns": ["account", "amount"],
        "rows": rows,
        "row_count": len(rows),
        "currency_columns": ["amount"],
        "line_meta": meta,
    }
    spec = assemble_spec("R", [{"type": "table", "result_id": "r1"}], lambda rid: payload)
    chart = next(s for s in spec["sections"] if s["type"] == "chart")
    assert "Grand Total" not in chart["svg"]  # the grand-total bar never dwarfs drivers
    assert "Detail 19" in chart["svg"]  # the largest leaf is in
    assert "Detail 0" not in chart["svg"] or "Detail 09" in chart["svg"]  # smallest out


# ---------------------------------------------------------------------------
# Explicit chart over a statement payload: same leaf-driver exclusion.
# ---------------------------------------------------------------------------
def test_explicit_chart_over_statement_excludes_summaries():
    payload = _statement_payload()
    out = _resolve_data_section({"type": "chart", "result_id": "r1"}, lambda rid: payload)
    assert out["type"] == "chart"
    assert "Operating Activities" not in out["svg"]
    assert "Intercompany" in out["svg"]


# ---------------------------------------------------------------------------
# Descriptive titles: model-supplied label wins; deterministic fallback otherwise.
# ---------------------------------------------------------------------------
def test_table_label_titles_the_auto_chart():
    payload = _months_payload()
    spec = assemble_spec(
        "R",
        [{"type": "table", "result_id": "r1", "label": "Cash Balance Trend"}],
        lambda rid: payload,
    )
    chart = next(s for s in spec["sections"] if s["type"] == "chart")
    assert "Cash Balance Trend" in chart["svg"]


def test_explicit_chart_default_title_is_descriptive_not_chart():
    payload = _months_payload()
    out = _resolve_data_section({"type": "chart", "result_id": "r1"}, lambda rid: payload)
    assert ">Chart<" not in out["svg"]  # the junk default is gone
    assert "cash_balance" in out["svg"]  # derived from the data instead


def test_explicit_chart_type_is_respected_over_shape():
    # The model explicitly asked for a bar over a monthly table — explicit wins.
    payload = _months_payload()
    out = _resolve_data_section({"type": "chart", "result_id": "r1", "chart_type": "bar"}, lambda rid: payload)
    assert out["chart_type"] == "bar"


def test_schema_accepts_label_on_table_and_chart_sections():
    from app.schemas.report import parse_sections

    parsed = parse_sections(
        [
            {"type": "table", "result_id": "r1", "label": "Cash Balance Trend"},
            {"type": "chart", "result_id": "r1", "label": "Trend", "chart_type": "line"},
        ]
    )
    assert parsed[0].label == "Cash Balance Trend"
    assert parsed[1].label == "Trend"
