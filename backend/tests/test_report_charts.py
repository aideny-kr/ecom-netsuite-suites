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
