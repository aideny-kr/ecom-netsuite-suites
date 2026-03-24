"""Tests for chart extraction from agent response text."""


from app.schemas.chart import ChartData
from app.services.chat.chart_extractor import extract_charts

VALID_CHART_JSON = '{"chart_type": "bar", "title": "Revenue by Region", "x_axis": {"label": "Region", "key": "region"}, "y_axes": [{"label": "Revenue", "key": "revenue"}], "data": [{"region": "US", "revenue": 1000}, {"region": "EU", "revenue": 800}]}'

VALID_LINE_JSON = '{"chart_type": "line", "title": "Monthly Sales", "x_axis": {"label": "Month", "key": "month"}, "y_axes": [{"label": "Sales", "key": "sales"}], "data": [{"month": "Jan", "sales": 100}, {"month": "Feb", "sales": 120}]}'

VALID_PIE_JSON = '{"chart_type": "pie", "title": "Market Share", "x_axis": {"label": "Company", "key": "company"}, "y_axes": [{"label": "Share", "key": "share"}], "data": [{"company": "A", "share": 60}, {"company": "B", "share": 40}]}'


class TestExtractCharts:

    def test_extract_single_chart(self):
        text = f"Here are the results.\n<chart>{VALID_CHART_JSON}</chart>\nSummary follows."
        cleaned, charts = extract_charts(text)
        assert len(charts) == 1
        assert isinstance(charts[0], ChartData)

    def test_extract_no_chart(self):
        text = "Just a normal response with no charts."
        cleaned, charts = extract_charts(text)
        assert cleaned == text
        assert charts == []

    def test_extract_multiple_charts(self):
        text = f"Part 1\n<chart>{VALID_CHART_JSON}</chart>\nPart 2\n<chart>{VALID_LINE_JSON}</chart>\nEnd."
        cleaned, charts = extract_charts(text)
        assert len(charts) == 2

    def test_cleaned_text_removes_chart_tags(self):
        text = f"Before <chart>{VALID_CHART_JSON}</chart> After"
        cleaned, charts = extract_charts(text)
        assert "<chart>" not in cleaned
        assert "</chart>" not in cleaned
        assert "Before" in cleaned
        assert "After" in cleaned

    def test_chart_data_has_required_fields(self):
        text = f"<chart>{VALID_CHART_JSON}</chart>"
        _, charts = extract_charts(text)
        chart = charts[0]
        assert chart.chart_type == "bar"
        assert chart.title == "Revenue by Region"
        assert chart.x_axis.key == "region"
        assert len(chart.y_axes) == 1
        assert len(chart.data) == 2

    def test_chart_type_bar(self):
        text = f'<chart>{VALID_CHART_JSON}</chart>'
        _, charts = extract_charts(text)
        assert charts[0].chart_type == "bar"

    def test_chart_type_line(self):
        text = f'<chart>{VALID_LINE_JSON}</chart>'
        _, charts = extract_charts(text)
        assert charts[0].chart_type == "line"

    def test_chart_type_pie(self):
        text = f'<chart>{VALID_PIE_JSON}</chart>'
        _, charts = extract_charts(text)
        assert charts[0].chart_type == "pie"

    def test_chart_type_invalid_defaults_to_bar(self):
        bad_json = '{"chart_type": "sunburst", "title": "T", "x_axis": {"label": "X", "key": "x"}, "y_axes": [{"label": "Y", "key": "y"}], "data": [{"x": 1, "y": 2}]}'
        text = f'<chart>{bad_json}</chart>'
        _, charts = extract_charts(text)
        assert len(charts) == 1
        assert charts[0].chart_type == "bar"

    def test_chart_data_array(self):
        text = f'<chart>{VALID_CHART_JSON}</chart>'
        _, charts = extract_charts(text)
        assert len(charts[0].data) == 2

    def test_malformed_json_skipped(self):
        text = "Before <chart>not json at all</chart> After"
        cleaned, charts = extract_charts(text)
        assert charts == []
        assert "<chart>" not in cleaned

    def test_partial_json_skipped(self):
        text = '<chart>{"chart_type": "bar"</chart>'
        cleaned, charts = extract_charts(text)
        assert charts == []
        assert "<chart>" not in cleaned

    def test_chart_options_parsed(self):
        json_with_opts = '{"chart_type": "bar", "title": "T", "x_axis": {"label": "X", "key": "x"}, "y_axes": [{"label": "Y", "key": "y"}], "data": [{"x": 1}], "options": {"stacked": true, "show_legend": false}}'
        text = f'<chart>{json_with_opts}</chart>'
        _, charts = extract_charts(text)
        assert charts[0].options is not None
        assert charts[0].options.stacked is True
        assert charts[0].options.show_legend is False

    def test_chart_options_optional(self):
        text = f'<chart>{VALID_CHART_JSON}</chart>'
        _, charts = extract_charts(text)
        assert charts[0].options is None
