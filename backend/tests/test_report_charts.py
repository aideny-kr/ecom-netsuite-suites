from app.schemas.chart import ChartAxis, ChartData
from app.services.report.report_charts import render_chart_svg


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
