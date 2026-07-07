from app.services.report.report_html import fmt_amount, render_report_html


def test_fmt_amount_accounting_style():
    """A currency cell renders accounting-style: thousands separators, 2 decimals
    (exact — foots, no precision loss), negatives in parentheses. None / non-finite →
    empty; non-numbers (and bools) pass through untouched."""
    assert fmt_amount(5583749.13) == "5,583,749.13"
    assert fmt_amount(-4595824.06766871) == "(4,595,824.07)"
    assert fmt_amount(0) == "0.00"
    assert fmt_amount(-0.004) == "0.00"  # tiny residual rounds to a clean zero, NOT "(0.00)"
    assert fmt_amount(None) == ""
    assert fmt_amount(float("nan")) == ""
    assert fmt_amount(float("inf")) == ""
    assert fmt_amount("Cash") == "Cash"  # non-numeric label untouched
    assert fmt_amount(True) == "True"  # bool is not a financial amount
    # numeric STRINGS (scientific notation + US thousands-grouping) are coerced —
    # SuiteQL serializes amounts as strings like "1.64...E7"
    assert fmt_amount("1.6442836348665524E7") == "16,442,836.35"
    assert fmt_amount("5,583,749.13") == "5,583,749.13"
    assert fmt_amount("-100.5") == "(100.50)"
    # STRICT coercion: a string we can't safely parse as a US-format amount passes
    # through VERBATIM — never mangled into a wrong (or blank) dollar figure.
    assert fmt_amount("N/A") == "N/A"  # non-numeric
    assert fmt_amount("1_000") == "1_000"  # underscore (float() would read 1000)
    assert fmt_amount("1.234,56") == "1.234,56"  # European locale grouping
    assert fmt_amount("1,2,3") == "1,2,3"  # mis-grouped
    assert fmt_amount("inf") == "inf"  # sentinel token, not blanked
    assert fmt_amount("1e400") == "1e400"  # out-of-double-range → verbatim, NOT blank
    assert fmt_amount("0042") == "0042"  # zero-padded code, NOT a $42.00 amount
    # EXACT cents via Decimal — binary float() would corrupt these:
    assert fmt_amount("999999999999999.99") == "999,999,999,999,999.99"  # float → ...000.00
    assert fmt_amount("2.675") == "2.68"  # half-cent rounds up; float("2.675") → 2.67
    # a large-but-finite number must FORMAT, never blank (default Decimal prec would)
    assert fmt_amount(1e26) == "100,000,000,000,000,000,000,000,000.00"
    # an absurd >309-digit int must NOT crash (f"{int:,.2f}" raises OverflowError);
    # it falls back to its exact raw repr, non-blank.
    assert fmt_amount(10**400) == str(10**400)


def test_only_tagged_currency_columns_are_accounting_formatted():
    """The accounting format is scoped to producer-tagged currency columns. A
    co-present non-currency numeric column (year, ratio, count) renders RAW — never
    comma-grouped or rounded (the renderer is shared infra; 'is a number' ≠ 'is a
    dollar amount')."""
    spec = {
        "title": "Cash Flow",
        "sections": [
            {
                "type": "table",
                "columns": ["account", "year", "ratio", "amount"],
                "rows": [["Net Income", 2024, 0.4523, 5583749.13], ["Op Activities", 2025, 0.51, -4595824.07]],
                "currency_columns": ["amount"],
                "row_count": 2,
            }
        ],
        "provenance": {},
    }
    html = render_report_html(spec)
    # currency column → accounting-formatted + right-aligned
    assert "5,583,749.13" in html
    assert "(4,595,824.07)" in html
    assert 'class="num"' in html
    # non-currency numerics are NOT mangled
    assert "<td>2024</td>" in html  # year: no comma grouping
    assert "2,024" not in html
    assert "<td>0.4523</td>" in html  # ratio: full precision, not rounded to 0
    assert "<td>Net Income</td>" in html  # string label untouched


def test_table_renders_overwide_row_cells_no_silent_drop():
    """A row WIDER than the declared columns must not have its trailing values silently
    dropped (that would hide a figure on a financial surface) — render every cell."""
    spec = {
        "title": "T",
        "sections": [{"type": "table", "columns": ["Account", "Amount"], "rows": [["Revenue", 100, 99999.99]]}],
        "provenance": {},
    }
    html = render_report_html(spec)
    assert "99999.99" in html  # the extra trailing value is rendered, not dropped


def test_table_without_currency_tag_formats_nothing():
    """Back-compat: an untagged table (SuiteQL/BigQuery/etc.) renders every cell raw —
    no accounting format is guessed onto generic numeric columns."""
    spec = {
        "title": "T",
        "sections": [{"type": "table", "columns": ["a", "b"], "rows": [["x", 1234567.5]], "row_count": 1}],
        "provenance": {},
    }
    html = render_report_html(spec)
    assert "1234567.5" in html  # raw — not "1,234,567.50"


def test_null_currency_cell_renders_empty_not_none():
    spec = {
        "title": "T",
        "sections": [
            {
                "type": "table",
                "columns": ["account", "amount"],
                "rows": [["Header", None]],
                "currency_columns": ["amount"],
                "row_count": 1,
            }
        ],
        "provenance": {},
    }
    html = render_report_html(spec)
    assert "None" not in html  # a null amount renders as an empty cell, not "None"


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


# --- Freshness stamp (Slice B: refresh honesty footer) -------------------------------


def _min_spec():
    return {"title": "R", "sections": [{"type": "narrative", "markdown": "hello"}]}


def test_no_freshness_keeps_output_stamp_free():
    """Golden safety: the compose path (freshness=None) is byte-identical — no stamp."""
    html = render_report_html(_min_spec())
    assert 'class="stamp"' not in html


def test_freshness_renders_composed_and_refreshed_dates():
    html = render_report_html(
        _min_spec(),
        freshness={"composed_at": "2026-07-06T18:04:12.331209+00:00", "refreshed_at": "2026-07-07T03:15:00+00:00"},
    )
    assert html.count('class="stamp"') == 1
    assert "Narrative composed 6 Jul 2026" in html
    assert "Data refreshed 7 Jul 2026" in html
    assert "UTC" in html


def test_freshness_values_are_escaped_and_unparseable_dates_never_crash():
    html = render_report_html(
        _min_spec(),
        freshness={"composed_at": "<script>alert(1)</script>", "refreshed_at": "not-a-date"},
    )
    assert "<script>" not in html  # hostile value neutralized
    assert "&lt;script&gt;" in html  # rendered verbatim-escaped, never dropped silently
    assert "not-a-date" in html
