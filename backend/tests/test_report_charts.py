import json

from app.schemas.chart import ChartAxis, ChartData
from app.services.report.report_charts import render_chart_svg
from app.services.report.report_service import _resolve_data_section


def _bar():
    return ChartData(
        chart_type="bar",
        title="Rev",
        x_axis=ChartAxis(label="P", key="period"),
        y_axes=[ChartAxis(label="Revenue", key="revenue", color="#6366f1")],
        data=[{"period": "Q1", "revenue": 100}, {"period": "Q2", "revenue": 150}],
    )


def test_bar_renders_svg():
    svg = render_chart_svg(_bar())
    assert svg.startswith("<svg") and "</svg>" in svg
    assert "Q1" in svg and "Q2" in svg  # x labels present
    assert "<rect" in svg  # bars drawn


def test_deterministic():
    assert render_chart_svg(_bar()) == render_chart_svg(_bar())


def test_bar_renders_negative_values_without_invalid_rects():
    """Financial data (cash flow, P&L) is full of negatives, and the auto-chart now
    plots it. A negative datum must render as a valid downward bar from a zero baseline,
    NOT a negative-height <rect> (invalid SVG) or an inverted/mis-scaled bar."""
    import re

    chart = ChartData(
        chart_type="bar",
        title="Drivers",
        x_axis=ChartAxis(label="account", key="account"),
        y_axes=[ChartAxis(label="amount", key="amount", color="#6366f1")],
        data=[
            {"account": "Revenue", "amount": 100.0},
            {"account": "Expenses", "amount": -250.0},
            {"account": "Other", "amount": 50.0},
        ],
    )
    svg = render_chart_svg(chart)
    assert svg.startswith("<svg") and "</svg>" in svg
    assert 'height="-' not in svg  # no negative-height rects
    # every bar rect stays within the SVG viewport (valid, non-negative geometry)
    for m in re.finditer(r'<rect [^>]*y="(-?[\d.]+)"[^>]*height="(-?[\d.]+)"', svg):
        y, h = float(m.group(1)), float(m.group(2))
        assert h >= 0
        assert -1 <= y <= 381 and y + h <= 381  # _H == 380, small tolerance


def test_all_zero_series_baseline_at_bottom_not_top():
    # A zero-activity period (all amounts 0) must draw its baseline at the BOTTOM with
    # zero-height bars, not collapse the baseline to the top of the plot.
    import re

    chart = ChartData(
        chart_type="bar",
        title="Zero",
        x_axis=ChartAxis(label="k", key="k"),
        y_axes=[ChartAxis(label="v", key="v", color="#6366f1")],
        data=[{"k": "A", "v": 0.0}, {"k": "B", "v": 0.0}],
    )
    svg = render_chart_svg(chart)
    m = re.search(r'<line x1="[\d.]+" y1="([\d.]+)"', svg)  # the baseline axis line (x1 now dynamic w/ left-pad)
    assert m
    assert float(m.group(1)) > 300  # near the bottom (plot bottom ~324), not _PAD_T (48)


def test_num_coerces_non_finite_to_zero():
    from app.services.report.report_charts import _num

    assert _num({"v": float("nan")}, "v") == 0.0
    assert _num({"v": float("inf")}, "v") == 0.0
    assert _num({"v": float("-inf")}, "v") == 0.0
    assert _num({"v": 42}, "v") == 42.0


def test_pie_handles_negative_values_without_nan():
    chart = ChartData(
        chart_type="pie",
        title="Pie",
        x_axis=ChartAxis(label="k", key="k"),
        y_axes=[ChartAxis(label="v", key="v")],
        data=[{"k": "A", "v": 100.0}, {"k": "B", "v": -50.0}],
    )
    svg = render_chart_svg(chart)
    assert svg.startswith("<svg")
    assert "nan" not in svg.lower()  # negative fractions must not produce NaN arc coordinates
    assert svg.count("<path") == 2  # two real magnitude slices (|100|, |50|), neither degenerate


def test_pie_single_slice_renders_full_circle():
    # A single slice (or one slice ~100%) is a degenerate arc (coincident endpoints) — it
    # must render as a full <circle>, not a blank/empty path.
    chart = ChartData(
        chart_type="pie",
        title="One",
        x_axis=ChartAxis(label="k", key="k"),
        y_axes=[ChartAxis(label="v", key="v")],
        data=[{"k": "A", "v": 100.0}],
    )
    svg = render_chart_svg(chart)
    assert "<circle" in svg


def test_all_negative_bar_series_renders_valid_bars():
    chart = ChartData(
        chart_type="bar",
        title="AllNeg",
        x_axis=ChartAxis(label="k", key="k"),
        y_axes=[ChartAxis(label="v", key="v", color="#6366f1")],
        data=[{"k": "A", "v": -10.0}, {"k": "B", "v": -40.0}],
    )
    svg = render_chart_svg(chart)
    assert 'height="-' not in svg
    assert svg.count('fill="#6366f1"') == 2  # both bars drawn


def test_unsupported_type_is_placeholder_not_crash():
    c = _bar()
    c.chart_type = "histogram"
    svg = render_chart_svg(c)
    assert "<svg" in svg and "not yet supported" in svg.lower()


def _real_metric_payload():
    from app.services.chat.tool_call_results import extract_result_payload
    from app.services.metrics.metric_compute import metric_data_table

    table = metric_data_table(
        "Net Revenue", "142800", "USD", "Q2 2026", "net_revenue", definition_version=7, source_kind="suiteql"
    )
    return extract_result_payload("metric_compute", {}, json.dumps(table))


def test_chart_on_metric_payload_returns_error_section():
    """Gate C (finding #4): a single-number metric table (cols Metric/Value/Unit/Period)
    has only ONE numeric column (Value); charting Unit/Period via float('USD')/float('Q2')
    silently plots zero-height nonsense bars. Charting a metric payload must instead
    return a deterministic error section, not a misleading frozen chart."""
    payload = _real_metric_payload()
    section = {"type": "chart", "result_id": "r1"}
    out = _resolve_data_section(section, lambda rid: payload)
    assert out["type"] == "error"
    assert "headline" in out["reason"].lower()


def test_chart_on_mixed_table_uses_only_numeric_columns():
    """Gate C: a tabular payload with a label column + a numeric column must chart ONLY
    the numeric column as a y-axis — never the non-numeric one (which would float()->0.0)."""
    payload = {
        "columns": ["Period", "Revenue", "Note"],
        "rows": [["Q1", "$1,000", "ok"], ["Q2", "1500", "ok"]],
    }
    out = _resolve_data_section({"type": "chart", "result_id": "r1"}, lambda rid: payload)
    assert out["type"] == "chart"
    assert out["svg"].startswith("<svg")
    # Only the ONE numeric column ('Revenue') is a series -> 2 rows x 1 series = 2 filled
    # bars in the first palette color. The buggy cols[1:] fallback would also add the
    # non-numeric 'Note' series (float('ok')->0.0 zero-height bars) in a 2nd palette color.
    assert out["svg"].count('fill="#6366f1"') == 2  # series 1 (Revenue) x 2 rows
    assert out["svg"].count('fill="#ef4444"') == 0  # no 2nd (non-numeric Note) series
    # '$1,000' parsed to 1000 (not float()->0.0): vmax is 1500 -> '1.5K' axis label.
    assert "1.5K" in out["svg"]


def test_chart_on_all_nonnumeric_table_returns_error_section():
    """Gate C: a table with NO numeric columns cannot be charted -> error section."""
    payload = {"columns": ["Country", "Status"], "rows": [["US", "open"], ["UK", "closed"]]}
    out = _resolve_data_section({"type": "chart", "result_id": "r1"}, lambda rid: payload)
    assert out["type"] == "error"
    assert "numeric" in out["reason"].lower()


def test_chart_column_numeric_probe_is_column_wide_not_row0():
    """Re-gate r2 (finding #1/#7): numeric-column detection must probe the WHOLE column,
    not just rows[0]. A column that is NULL/blank in the first row but numeric in later
    rows is a valid y-axis (e.g. an opening period with no revenue yet). Probing only
    rows[0] drops it -> the real metric is silently omitted from the published chart."""
    # 12-row table, Revenue is NULL in row 0 but numeric in rows 1-11.
    rows = [["P0", None]] + [[f"P{i}", str(i * 100)] for i in range(1, 12)]
    payload = {"columns": ["Period", "Revenue"], "rows": rows}
    out = _resolve_data_section({"type": "chart", "result_id": "r1"}, lambda rid: payload)
    # Revenue IS charted (column-wide probe finds the 11 non-null numeric cells).
    assert out["type"] == "chart"
    assert out["svg"].startswith("<svg")
    # The Revenue series renders bars in the first palette color (row 0's NULL coerces to
    # a zero-height bar, but the column still qualifies and the real values plot).
    assert 'fill="#6366f1"' in out["svg"]
    # vmax = 1100 (P11 = 11*100) -> '1.1K' axis label proves the real values were charted.
    assert "1.1K" in out["svg"]


def test_chart_column_numeric_probe_coerces_null_cells_to_zero():
    """Re-gate r2: once a column qualifies as numeric, a non-parsing cell (NULL in row 0)
    must coerce to 0.0 so the renderer plots a real (zero-height) bar, never crashes."""
    rows = [["P0", None], ["P1", "500"]]
    payload = {"columns": ["Period", "Revenue"], "rows": rows}
    out = _resolve_data_section({"type": "chart", "result_id": "r1"}, lambda rid: payload)
    assert out["type"] == "chart"
    # 2 rows x 1 series -> 2 bars in palette[0]; the NULL row plots a 0-height bar.
    assert out["svg"].count('fill="#6366f1"') == 2


def test_chart_caps_rows_at_max_chart_points():
    """Re-gate r2 (finding #12): charts have NO row cap, so a 50k-row payload bakes a
    multi-MB SVG into the report. A payload with > _MAX_CHART_POINTS rows must return a
    deterministic error section telling the model to aggregate first."""
    from app.services.report import report_service

    cap = report_service._MAX_CHART_POINTS
    assert cap == 100
    over = {
        "columns": ["Period", "Revenue"],
        "rows": [[str(i), str(i * 10)] for i in range(cap + 1)],
    }
    out = _resolve_data_section({"type": "chart", "result_id": "r1"}, lambda rid: over)
    assert out["type"] == "error"
    assert "too many rows" in out["reason"].lower()
    assert str(cap + 1) in out["reason"]


def test_chart_at_max_chart_points_still_charts():
    """Re-gate r2: exactly _MAX_CHART_POINTS rows is fine -> a real chart, not an error."""
    from app.services.report import report_service

    cap = report_service._MAX_CHART_POINTS
    at = {
        "columns": ["Period", "Revenue"],
        "rows": [[str(i), str(i * 10)] for i in range(cap)],
    }
    out = _resolve_data_section({"type": "chart", "result_id": "r1"}, lambda rid: at)
    assert out["type"] == "chart"
    assert out["svg"].startswith("<svg")


def _injected_bar(malicious_color: str) -> ChartData:
    return ChartData(
        chart_type="bar",
        title="Rev",
        x_axis=ChartAxis(label="P", key="period"),
        y_axes=[ChartAxis(label="Revenue", key="revenue", color=malicious_color)],
        data=[{"period": "Q1", "revenue": 100}, {"period": "Q2", "revenue": 150}],
    )


def test_bar_malicious_color_falls_back_to_palette_no_script():
    """Gate D (finding #20): ChartAxis.color is interpolated raw into SVG fill="...".
    A crafted color must NOT break out of the attribute — it must be rejected and the
    palette default substituted, and the rendered SVG must contain NO '<script'."""
    svg = render_chart_svg(_injected_bar('"/><script>alert(1)</script>'))
    assert "<script" not in svg
    assert "alert(1)" not in svg
    # The injection is rejected -> series 0 falls back to palette[0].
    assert 'fill="#6366f1"' in svg


def test_line_malicious_color_falls_back_to_palette_no_script():
    """Gate D: lines/areas/points interpolate color too -> same validation must apply."""
    c = _injected_bar('#fff"/><script>x</script>')
    c.chart_type = "line"
    svg = render_chart_svg(c)
    assert "<script" not in svg
    assert 'stroke="#6366f1"' in svg


def test_area_malicious_color_falls_back_to_palette_no_script():
    c = _injected_bar("red onload=alert(1)")
    c.chart_type = "area"
    svg = render_chart_svg(c)
    assert "<script" not in svg
    assert "onload" not in svg
    # The fill-opacity polygon + polyline + point rects all use the palette default.
    assert 'fill="#6366f1"' in svg


def test_valid_hex_and_hsl_colors_are_preserved():
    """Gate D: legitimate colors (#rgb / #rrggbb / #rrggbbaa / hsl()/hsla()) pass through."""
    svg = render_chart_svg(_injected_bar("#abc"))
    assert 'fill="#abc"' in svg
    svg = render_chart_svg(_injected_bar("#12ab34cd"))
    assert 'fill="#12ab34cd"' in svg
    svg = render_chart_svg(_injected_bar("hsl(210, 50%, 40%)"))
    assert 'fill="hsl(210, 50%, 40%)"' in svg
    svg = render_chart_svg(_injected_bar("hsla(210, 50%, 40%, 0.5)"))
    assert 'fill="hsla(210, 50%, 40%, 0.5)"' in svg


# --- Slice D: series groups, datum tooltips, legend markup ------------------------------
# Spec §4D interactivity is CSS-only (the FE viewer iframe is sandbox="" — no scripts):
# per-series <g class="ser-j"> hooks for the checkbox-toggle CSS in report_html._CSS,
# native SVG <title> hover values (browser-rendered, works sandboxed), and a legend
# appended AFTER </svg> in the same returned string (one self-contained artifact).


def _multi_bar():
    return ChartData(
        chart_type="bar",
        title="Rev vs Cost",
        x_axis=ChartAxis(label="P", key="period"),
        y_axes=[
            ChartAxis(label="Revenue", key="revenue", color="#6366f1"),
            ChartAxis(label="Cost", key="cost"),
        ],
        data=[
            {"period": "Q1", "revenue": 1234567.5, "cost": 200},
            {"period": "Q2", "revenue": 150, "cost": 90},
        ],
    )


def test_multiseries_bar_wraps_each_series_in_classed_group():
    svg = render_chart_svg(_multi_bar())
    assert 'class="ser-0"' in svg and 'class="ser-1"' in svg


def test_bar_datapoint_has_value_tooltip():
    """Native SVG <title> = the hover value. Full-precision thousands-separated figure
    (the tooltip is where exact numbers belong — never the '1.2M' axis abbreviation)."""
    svg = render_chart_svg(_multi_bar())
    assert "<title>Q1 — Revenue: 1,234,567.5</title>" in svg
    assert "<title>Q2 — Cost: 90</title>" in svg  # integral floats render without '.0'


def test_line_points_and_pie_slices_have_value_tooltips():
    line = ChartData(
        chart_type="line",
        title="Trend",
        x_axis=ChartAxis(label="P", key="period"),
        y_axes=[ChartAxis(label="Revenue", key="revenue")],
        data=[{"period": "Q1", "revenue": 100}, {"period": "Q2", "revenue": 150}],
    )
    assert "<title>Q1 — Revenue: 100</title>" in render_chart_svg(line)
    pie = ChartData(
        chart_type="pie",
        title="Mix",
        x_axis=ChartAxis(label="seg", key="seg"),
        y_axes=[ChartAxis(label="Amount", key="amount")],
        data=[{"seg": "Ops", "amount": 60}, {"seg": "R&D", "amount": 40}],
    )
    svg = render_chart_svg(pie)
    assert "<title>Ops — Amount: 60</title>" in svg
    assert "<title>R&amp;D — Amount: 40</title>" in svg  # category text is escaped


def test_single_series_chart_has_no_legend():
    """A one-entry legend is noise, and toggling the only series off would blank the
    chart — single-series output carries no legend at all."""
    assert "chart-legend" not in render_chart_svg(_bar())


def test_multiseries_chart_appends_checkbox_legend_after_svg():
    svg = render_chart_svg(_multi_bar())
    assert svg.startswith("<svg")  # legend rides AFTER the svg, same returned string
    legend = svg.split("</svg>", 1)[1]
    assert 'class="chart-legend"' in legend
    assert legend.count('<input type="checkbox"') == 2
    assert legend.count("checked") == 2  # all series start visible
    assert "Revenue" in legend and "Cost" in legend
    assert 'style="background:#6366f1"' in legend  # swatch mirrors the series color


def test_pie_legend_is_static_no_checkboxes():
    """Pie slices are categories, not series: CSS-hiding a slice would leave a
    wedge-shaped hole that misrepresents the remaining shares (angles can't be
    recomputed without JS) — the legend is the color→label key only."""
    pie = ChartData(
        chart_type="pie",
        title="Mix",
        x_axis=ChartAxis(label="seg", key="seg"),
        y_axes=[ChartAxis(label="Amount", key="amount")],
        data=[{"seg": "Ops", "amount": 60}, {"seg": "R&D", "amount": 40}],
    )
    svg = render_chart_svg(pie)
    legend = svg.split("</svg>", 1)[1]
    assert 'class="chart-legend"' in legend
    assert "<input" not in legend
    assert "Ops" in legend and "R&amp;D" in legend


def test_legend_swatch_malicious_color_falls_back_to_palette():
    """Gate D lineage: ChartAxis.color is upstream-influenced; the legend swatch style
    attribute must route through _safe_color exactly like the series fill."""
    hostile = _multi_bar()
    hostile.y_axes[0].color = '"><script>alert(1)</script>'
    svg = render_chart_svg(hostile)
    assert "<script" not in svg
    assert 'style="background:#6366f1"' in svg  # palette default for series 0


def test_chart_output_has_no_ids():
    """The legend uses label-WRAPPED inputs precisely so no id/for pairs exist — a
    report with several charts would otherwise collide ids (invalid HTML, broken
    toggles) and ids would threaten render determinism."""
    assert 'id="' not in render_chart_svg(_multi_bar())


def test_multiseries_output_is_deterministic():
    assert render_chart_svg(_multi_bar()) == render_chart_svg(_multi_bar())
