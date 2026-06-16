from app.services.report.report_html import render_report_html


def test_render_self_contained_html():
    spec = {
        "title": "Q2 Review",
        "generated_at": "2026-06-10T00:00:00Z",
        "sections": [
            {"type": "heading", "level": 1, "text": "Q2 Review"},
            {"type": "narrative", "markdown": "Revenue grew **12%** this quarter."},
            {
                "type": "metric_headline",
                "label": "Revenue",
                "value": "1.2M",
                "unit": "USD",
                "period": "Q2",
                "definition_version": 3,
            },
            {"type": "chart", "svg": "<svg id='c1'></svg>"},
            {"type": "table", "columns": ["Period", "Revenue"], "rows": [["Q1", "100"], ["Q2", "150"]], "row_count": 2},
            {"type": "divider"},
        ],
        "provenance": {"sources": ["metric:revenue@v3"]},
    }
    html = render_report_html(spec, accent_hsl="142 70% 45%")
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "<style>" in html  # inline CSS, self-contained
    assert "Q2 Review" in html
    assert "<svg id='c1'></svg>" in html  # chart svg embedded verbatim
    assert "150" in html  # table value
    assert "definition" in html.lower()  # provenance footnote rendered


def test_html_escapes_user_text():
    spec = {"title": "<script>x</script>", "sections": [], "provenance": {}}
    html = render_report_html(spec, accent_hsl="0 0% 0%")
    assert "<script>x</script>" not in html  # escaped


def test_narrative_renders_gfm_table_as_html_table():
    # The composer emits GFM markdown tables inside narrative content. They must
    # render as a real <table>, not a wall of literal pipes.
    md = "| Currency | FX Rate | Rounding |\n|---|---|---|\n| AUD | 1.50 | nearest_9 |\n| BGN | 1.95583 | nearest_9 |\n"
    spec = {"title": "Pricing", "sections": [{"type": "narrative", "markdown": md}], "provenance": {}}
    html = render_report_html(spec)
    assert "<table>" in html
    assert "<th>Currency</th>" in html
    assert "<td>nearest_9</td>" in html
    # The delimiter row must NOT leak into output as literal text.
    assert "|---|" not in html
    # No raw pipe-delimited header row left dumped as text.
    assert "| Currency | FX Rate |" not in html


def test_narrative_table_cells_are_escaped():
    # Trust boundary: cell content is LLM-authored — must be escaped, no raw HTML.
    md = "| Col |\n|---|\n| <script>x</script> |\n"
    spec = {"title": "T", "sections": [{"type": "narrative", "markdown": md}], "provenance": {}}
    html = render_report_html(spec)
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


def test_narrative_splits_paragraphs_around_table():
    md = "Intro line.\n\n| A |\n|---|\n| 1 |\n\nClosing **note**."
    spec = {"title": "T", "sections": [{"type": "narrative", "markdown": md}], "provenance": {}}
    html = render_report_html(spec)
    assert "<p>Intro line.</p>" in html
    assert "<table>" in html
    assert "<strong>note</strong>" in html
    # prose and the table are distinct blocks, not one run-on line.
    assert "Intro line. |" not in html


def test_narrative_thematic_break_does_not_eat_piped_prose():
    # A prose line containing a pipe, followed by a `---` horizontal rule, must
    # NOT be mistaken for a one-row table (GFM delimiter rows contain pipes).
    md = "See the A | B comparison.\n---\nMore prose."
    spec = {"title": "T", "sections": [{"type": "narrative", "markdown": md}], "provenance": {}}
    html = render_report_html(spec)
    # The piped sentence survives as prose, not destroyed into an empty table.
    assert "comparison" in html
    assert "<tbody></tbody>" not in html
    assert "<th>See the A</th>" not in html


def test_narrative_ragged_rows_normalized_to_header_width():
    # Short body rows are padded, over-long rows truncated, to the header width.
    md = "| A | B | C |\n|---|---|---|\n| 1 | 2 |\n| x | y | z | w |\n"
    spec = {"title": "T", "sections": [{"type": "narrative", "markdown": md}], "provenance": {}}
    html = render_report_html(spec)
    # Every body row has exactly 3 <td> (header width); the stray 'w' is dropped.
    assert ">w<" not in html
    first_row = html.split("</thead>")[1]
    assert first_row.count("<tr>") == 2
    for tr in first_row.split("<tr>")[1:]:
        assert tr.count("<td>") == 3


def test_narrative_single_newline_reflows_not_hard_break():
    # Single newlines inside a paragraph reflow (join with space), matching the
    # prior whitespace-collapsing behavior — no injected <br>.
    md = "The quarter closed strong\nwith revenue up 12%."
    spec = {"title": "T", "sections": [{"type": "narrative", "markdown": md}], "provenance": {}}
    html = render_report_html(spec)
    assert "<p>The quarter closed strong with revenue up 12%.</p>" in html
    assert "<br>" not in html
