from app.services.report.report_html import render_report_html

def test_render_self_contained_html():
    spec = {"title": "Q2 Review", "generated_at": "2026-06-10T00:00:00Z", "sections": [
        {"type": "heading", "level": 1, "text": "Q2 Review"},
        {"type": "narrative", "markdown": "Revenue grew **12%** this quarter."},
        {"type": "metric_headline", "label": "Revenue", "value": "1.2M", "unit": "USD", "period": "Q2", "definition_version": 3},
        {"type": "chart", "svg": "<svg id='c1'></svg>"},
        {"type": "table", "columns": ["Period", "Revenue"], "rows": [["Q1", "100"], ["Q2", "150"]], "row_count": 2},
        {"type": "divider"},
    ], "provenance": {"sources": ["metric:revenue@v3"]}}
    html = render_report_html(spec, accent_hsl="142 70% 45%")
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "<style>" in html                      # inline CSS, self-contained
    assert "Q2 Review" in html
    assert "<svg id='c1'></svg>" in html          # chart svg embedded verbatim
    assert "150" in html                           # table value
    assert "definition" in html.lower()            # provenance footnote rendered

def test_html_escapes_user_text():
    spec = {"title": "<script>x</script>", "sections": [], "provenance": {}}
    html = render_report_html(spec, accent_hsl="0 0% 0%")
    assert "<script>x</script>" not in html        # escaped
