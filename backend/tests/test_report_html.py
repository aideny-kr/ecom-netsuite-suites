import re
from html import escape

from app.services.report.report_html import build_provenance, fmt_amount, render_report_html
from app.services.report.statement_builder import build_statement_model
from tests.fixtures import statement_fixture as fx


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


def test_freshness_with_empty_refreshed_at_omits_dangling_separator():
    """Compose-time freshness (playbook compose) passes refreshed_at="" — the stamp must
    render ONLY the composed part, with no trailing '· Data refreshed' separator/label
    for a value that was never set."""
    html = render_report_html(
        _min_spec(),
        freshness={"composed_at": "2026-07-06T18:04:12.331209+00:00", "refreshed_at": ""},
    )
    assert "Narrative composed 6 Jul 2026" in html
    assert "Data refreshed" not in html
    assert html.count('class="stamp"') == 1


def test_freshness_values_are_escaped_and_unparseable_dates_never_crash():
    html = render_report_html(
        _min_spec(),
        freshness={"composed_at": "<script>alert(1)</script>", "refreshed_at": "not-a-date"},
    )
    assert "<script>" not in html  # hostile value neutralized
    assert "&lt;script&gt;" in html  # rendered verbatim-escaped, never dropped silently
    assert "not-a-date" in html


# --- Slice D: CSS-only interactivity (the FE viewer iframe is sandbox="" — no scripts;
# every §4D feature must work as pure CSS/markup riding inside rendered_html) -----------


def _slice_d_spec():
    return {
        "title": "D",
        "sections": [
            {"type": "table", "columns": ["a", "amount"], "rows": [["x", 1]], "currency_columns": ["amount"]},
            {"type": "chart", "svg": "<svg></svg>"},
        ],
    }


def test_css_toggle_rules_cover_every_toggleable_series_index():
    """The checkbox-toggle rules bind the legend inputs (class ser-j, emitted by
    report_charts._legend) to their series groups via :has() — no ids, no JS.
    DRIFT GUARD (review r1): the legend emits checkboxes for j < _MAX_TOGGLE_SERIES;
    a rule missing for any such j makes that checkbox a dead control, so the CSS
    block must cover exactly the constant."""
    from app.services.report.report_charts import _MAX_TOGGLE_SERIES

    html = render_report_html(_slice_d_spec())
    assert ":has(" in html
    for j in range(_MAX_TOGGLE_SERIES):
        assert f"input.ser-{j}:not(:checked)" in html, f"toggle rule missing for ser-{j}"
    assert f"input.ser-{_MAX_TOGGLE_SERIES}:" not in html  # block ends at the cap


def test_table_card_gets_table_wrap_class_and_sticky_css():
    """Sticky thead can only engage inside the card's own scroll box: the overflow-x
    wrapper forces computed overflow-y, so document-relative sticky never fires — the
    table card gains a capped-height scroll region instead. Chart/narrative cards are
    untouched."""
    html = render_report_html(_slice_d_spec())
    assert 'class="nb-card svg-wrap table-wrap"' in html
    assert "position:sticky" in html
    assert "max-height:70vh" in html
    assert '<div class="nb-card svg-wrap"><svg></svg></div>' in html  # chart card unchanged


def test_stamp_css_rule_defined():
    """The Slice-B freshness stamp rendered UNSTYLED (class=\"stamp\" had no rule) —
    style it like the provenance footer. Content assertions elsewhere are untouched."""
    html = render_report_html(
        _slice_d_spec(),
        freshness={"composed_at": "2026-07-06T18:00:00+00:00", "refreshed_at": "2026-07-07T18:00:00+00:00"},
    )
    assert ".stamp {" in html
    assert 'class="stamp"' in html


def test_render_report_html_deterministic():
    spec = _slice_d_spec()
    assert render_report_html(spec) == render_report_html(spec)


def test_print_stylesheet_present_and_defuses_screen_features():
    """Greenfield @media print: un-clip the scroll regions (sticky prints frozen and
    overflow-y clips rows off the page), keep card colors where the engine honors
    print-color-adjust, hide the legend checkbox WIDGETS (swatch+label stay — the
    printed page shows what was toggled on, WYSIWYG), let long tables paginate."""
    html = render_report_html(_slice_d_spec())
    assert "@media print" in html
    printed = html.split("@media print", 1)[1]
    assert "position:static" in printed  # defuse sticky
    assert "overflow:visible" in printed and "max-height:none" in printed  # un-clip
    assert "box-shadow:none" in printed
    assert "print-color-adjust:exact" in printed
    assert ".chart-legend input { display:none; }" in printed
    assert "break-inside:avoid" in printed  # cards don't split across pages


def test_document_is_self_contained_no_external_references():
    """Codifies the previously-untested §4D invariant: the artifact is ONE
    self-contained document — no CDN scripts, stylesheets, imports, or url() fetches.
    The only 'http' substring is the SVG xmlns namespace identifier (not a fetch)."""
    from app.schemas.chart import ChartAxis, ChartData
    from app.services.report.report_charts import render_chart_svg

    chart = ChartData(
        chart_type="bar",
        title="C",
        x_axis=ChartAxis(label="p", key="p"),
        y_axes=[ChartAxis(label="A", key="a"), ChartAxis(label="B", key="b")],
        data=[{"p": "Q1", "a": 1, "b": 2}],
    )
    html = render_report_html(
        {
            "title": "Self-contained",
            "sections": [
                {"type": "chart", "svg": render_chart_svg(chart)},
                {"type": "table", "columns": ["a"], "rows": [["x"]]},
                {"type": "narrative", "markdown": "All **inline**."},
            ],
            "provenance": {"sources": ["SuiteQL"]},
        },
        freshness={"composed_at": "2026-07-06T18:00:00+00:00", "refreshed_at": "2026-07-07T18:00:00+00:00"},
    )
    assert "<link" not in html
    assert "<script" not in html
    assert "@import" not in html
    assert "url(" not in html
    assert html.count("http") == 1  # the svg xmlns only


def test_multiseries_chart_size_canary():
    """Coarse byte tripwire, not a spec: every rendered_html byte is stored ~30x
    (version retention cap), so a tooltip/legend markup blowup must fail loudly.
    Baseline at authoring time: ~34KB for a 100-point 3-series line chart."""
    from app.schemas.chart import ChartAxis, ChartData
    from app.services.report.report_charts import render_chart_svg

    rows = [{"m": f"M{i:03d}", "a": i * 1000.5, "b": i * 2, "c": 7_000_000 - i} for i in range(100)]
    chart = ChartData(
        chart_type="line",
        title="Canary",
        x_axis=ChartAxis(label="m", key="m"),
        y_axes=[ChartAxis(label="A", key="a"), ChartAxis(label="B", key="b"), ChartAxis(label="C", key="c")],
        data=rows,
    )
    assert len(render_chart_svg(chart).encode()) < 80_000


def test_dark_accent_gets_light_table_header_ink():
    """Live QA (2026-07-09): th background is the tenant accent, and Framework's
    accent is near-black — header text (--ink #111) rendered dark-on-dark,
    illegible live AND in print. The header ink must be computed from the accent's
    lightness server-side (CSS alone can't derive contrast from an hsl() var)."""
    html = render_report_html(_slice_d_spec())  # default accent "240 6% 10%" — dark
    assert "--accent-ink:#fff" in html
    assert "color:var(--accent-ink)" in html  # th rule uses it


def test_light_accent_keeps_dark_table_header_ink():
    html = render_report_html(_slice_d_spec(), accent_hsl="48 96% 89%")
    assert "--accent-ink:#111" in html


def test_unparseable_accent_falls_back_to_dark_ink():
    html = render_report_html(_slice_d_spec(), accent_hsl="not-a-color")
    assert "--accent-ink:#111" in html


def test_print_table_header_pins_light_background_and_dark_ink():
    """Gate r1 on the contrast fix: engines that ignore print-color-adjust STRIP
    backgrounds — a computed light --accent-ink would then print white-on-white
    (invisible, worse than the original dark-on-dark). Print pins a light header
    background + dark ink so headers are legible on EVERY engine."""
    html = render_report_html(_slice_d_spec())
    printed = html.split("@media print", 1)[1]
    assert "th { position:static; background:#eee; color:var(--ink); }" in printed


# --- Provenance block ("Sources & method") --------------------------------------------


def _freshness():
    return {"composed_at": "2026-07-06T18:04:12.331209+00:00", "refreshed_at": "2026-07-07T03:15:00+00:00"}


def test_provenance_block_renders_sources_and_method():
    prov = build_provenance(
        {
            "r1": {
                "tool": "netsuite_financial_report",
                "params": {"report_type": "income_statement", "period": "Jun 2026"},
                "connection_id": None,
            },
            "r2": {
                "tool": "ext__fc1cba33e9924f62a5b7df0d5f235214__ns_runReport",
                "params": {"reportId": -203},
                "connection_id": "fc1cba33",
            },
        },
        executed_at="2026-07-17T20:00:00+00:00",
    )
    html = render_report_html(_min_spec(), freshness=_freshness(), provenance=prov)
    assert "Sources &amp; method" in html or "Sources & method" in html
    assert "NetSuite GL statement template (SuiteQL)" in html
    assert "NetSuite native report runner" in html
    assert "period=Jun 2026" in html
    assert "2026-07-17T20:00:00+00:00" in html


def test_no_provenance_renders_no_block():
    html = render_report_html(_min_spec(), freshness=_freshness())
    assert "Sources & method" not in html


def test_provenance_values_are_escaped():
    prov = [{"result_id": "r1", "label": "<script>x</script>", "detail": "a=<b>", "executed_at": "t"}]
    html = render_report_html(_min_spec(), freshness=_freshness(), provenance=prov)
    assert "<script>x</script>" not in html


def test_provenance_excludes_llm_only_params_and_raw_sql():
    """build_provenance must never leak a chat-composed recipe's raw suiteql params into
    the frozen HTML: `query` is the literal SQL text (the label already conveys method —
    never print SQL into a report), and `user_question` is verbatim chat text that is
    ALSO stripped before dispatch on refresh (_LLM_ONLY_PARAMS in refresh_service) —
    displaying it would misrepresent what was actually replayed. A benign param on the
    same source must still show."""
    prov = build_provenance(
        {
            "r1": {
                "tool": "netsuite_suiteql",
                "params": {
                    "query": "SELECT ssn, salary FROM employee",
                    "user_question": "what did the CEO get paid last year",
                    "limit": 50,
                },
                "connection_id": None,
            }
        },
        executed_at="2026-07-17T20:00:00+00:00",
    )
    detail = prov[0]["detail"]
    assert "SELECT" not in detail
    assert "salary" not in detail
    assert "CEO" not in detail
    assert "what did the CEO get paid" not in detail
    assert "limit=50" in detail
    # the label alone still conveys method — unaffected by the param policy
    assert prov[0]["label"] == "NetSuite SuiteQL query"
    html = render_report_html(_min_spec(), freshness=_freshness(), provenance=prov)
    assert "SELECT" not in html


def test_provenance_excludes_sql_from_external_custom_suiteql():
    """ext__<hex>__ns_runCustomSuiteQL is recipe-eligible (data_table category) and
    carries the full SQL under `sqlQuery` (the external-MCP equivalent of the local
    `query` key) — same leak class as Finding 1, different key name."""
    prov = build_provenance(
        {
            "r1": {
                "tool": "ext__fc1cba33e9924f62a5b7df0d5f235214__ns_runCustomSuiteQL",
                "params": {"sqlQuery": "SELECT ssn FROM employee", "limit": 50},
                "connection_id": "fc1cba33",
            }
        },
        executed_at="2026-07-17T20:00:00+00:00",
    )
    detail = prov[0]["detail"]
    assert "SELECT" not in detail
    assert "ssn" not in detail
    assert "limit=50" in detail


def test_provenance_excludes_sql_from_cross_source_query():
    """cross_source_query is recipe-eligible (data_table category) and takes TWO full
    SQL texts under left_query/right_query."""
    prov = build_provenance(
        {
            "r1": {
                "tool": "cross_source_query",
                "params": {
                    "left_query": "SELECT ssn FROM employee",
                    "right_query": "SELECT card_number FROM payments",
                    "join_type": "inner",
                },
                "connection_id": None,
            }
        },
        executed_at="2026-07-17T20:00:00+00:00",
    )
    detail = prov[0]["detail"]
    assert "SELECT" not in detail
    assert "ssn" not in detail
    assert "card_number" not in detail
    assert "join_type=inner" in detail


def test_provenance_detail_value_truncated_at_80_chars():
    """Forward guard: even a param NOT on the exclusion list must not blow up the
    frozen HTML with an unbounded value — caps any surviving detail value so a future
    recipe-eligible tool with a big text param under a new (unlisted) name can't
    silently reopen this leak class."""
    long_value = "x" * 200
    prov = build_provenance(
        {"r1": {"tool": "netsuite_suiteql", "params": {"note": long_value}, "connection_id": None}},
        executed_at="2026-07-17T20:00:00+00:00",
    )
    detail = prov[0]["detail"]
    assert long_value not in detail
    assert "…" in detail or "..." in detail
    assert len(detail) < 120  # "note=" + ~80 chars + ellipsis, well short of the full 200


# --- Task 3: `financial_statement` renderer (CFO-grade statement, CSS-only interactivity) --
#
# The renderer consumes ONLY the MODEL built by statement_builder.build_statement_model —
# every fixture below goes through the real builder (never a hand-written model dict) so
# these tests exercise the actual Task 1/2 -> Task 3 contract, not an assumption about it.


def _fs_spec(model: dict, title: str = "Income Statement") -> dict:
    return {"title": title, "sections": [{"type": "financial_statement", "model": model}], "provenance": {}}


def _is_model():
    return build_statement_model(fx.income_statement_section(), fx.income_statement_payloads())


def test_fs_report_has_exactly_one_h1():
    html = render_report_html(_fs_spec(_is_model()))
    assert html.count("<h1") == 1


def test_fs_kpi_values_and_deltas_render_with_formatted_strings():
    html = render_report_html(_fs_spec(_is_model()))
    assert "$13,500,000" in html  # revenue value
    assert fx.EXPECTED_REVENUE_MOM_DELTA_STR in html
    assert fx.EXPECTED_REVENUE_MOM_PCT_STR in html
    assert fx.EXPECTED_REVENUE_YOY_PCT_STR in html
    assert fx.EXPECTED_GP_MARGIN_STR in html  # gross profit KPI margin


def test_fs_kpi_sparkline_svg_present_for_is():
    html = render_report_html(_fs_spec(_is_model()))
    assert 'class="fs-spark"' in html
    assert "<polyline" in html


def test_fs_quad_rows_render_all_four_metrics():
    html = render_report_html(_fs_spec(_is_model()))
    assert 'class="fs-quad' in html
    for label in ("Revenue", "Gross Profit", "Operating Income", "Net Income"):
        assert f"<td>{label}</td>" in html


def test_fs_statement_emphasis_rows_present_with_derived_totals():
    html = render_report_html(_fs_spec(_is_model()))
    assert 'class="fs-sub' in html
    assert 'class="fs-formula"' in html
    assert 'class="fs-net"' in html
    assert "$3,500,000" in html  # gross profit (formula row)
    assert "$1,800,000" in html  # operating income (formula row)
    assert "$1,805,000" in html  # net income (net row)


def test_fs_statement_table_renders_every_fixture_account_no_truncation():
    html = render_report_html(_fs_spec(_is_model()))
    # 30 accounts in the fixture -- one <tr class="fs-acct ...> per account, no cap.
    # (the narrower "fs-acct " with a trailing space excludes the fs-acct-no muted-number
    # span class, which also starts with "fs-acct".)
    assert html.count('<tr class="fs-acct ') == 30


def test_fs_parens_formatting_present_for_reduces_profit_lines():
    html = render_report_html(_fs_spec(_is_model()))
    assert "($10,000,000)" in html  # COGS section subtotal


def test_fs_account_name_escaping_never_renders_raw():
    payloads = fx.income_statement_payloads()
    r1 = payloads["r1"]
    cols = r1["columns"]
    name_idx = cols.index("acctname")
    rows = [list(row) for row in r1["rows"]]
    rows[0][name_idx] = "<script>alert(1)</script>"
    payloads["r1"] = dict(r1, rows=rows)
    model = build_statement_model(fx.income_statement_section(), payloads)
    html = render_report_html(_fs_spec(model))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_fs_narrative_escaping_never_renders_raw():
    model = _is_model()
    model = dict(model, narrative=["<script>alert(2)</script>", *model["narrative"][1:]])
    html = render_report_html(_fs_spec(model))
    assert "<script>alert(2)</script>" not in html
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in html


def test_fs_no_script_tag_anywhere_in_rendered_output():
    html = render_report_html(_fs_spec(_is_model()))
    assert "<script" not in html


def test_fs_collapse_checkboxes_capped_and_css_rules_match_the_cap():
    """DRIFT GUARD (mirrors test_css_toggle_rules_cover_every_toggleable_series_index):
    the CSS :has() collapse rules must cover exactly fs-sec-0..fs-sec-{cap-1}."""
    from app.services.report.report_html import _MAX_STATEMENT_SECTIONS

    html = render_report_html(_fs_spec(_is_model()))
    assert ":has(" in html
    for i in range(_MAX_STATEMENT_SECTIONS):
        assert f"input.fs-sec-{i}:not(:checked)" in html, f"collapse rule missing for fs-sec-{i}"
    assert f"input.fs-sec-{_MAX_STATEMENT_SECTIONS}:" not in html  # block ends at the cap


def test_fs_delta_tone_cell_actually_gets_the_semantic_color_class_bound():
    """DRIFT GUARD: _fs_delta_tone / _fs_sign_tone hand back bare "fs-good"/"fs-bad"
    strings that get applied DIRECTLY as a <td> class on statement/quad delta cells
    (see _fs_summary_row_html / _fs_account_row_html / _fs_quad_row_with_pct_html) --
    not just as a compound class alongside .fs-chip/.fs-dot/.fs-delta. A bare
    `td.fs-good` only picks up color if the stylesheet defines an UNSCOPED base rule
    for the class itself; a stylesheet with only scoped companions (.fs-chip.fs-good,
    .fs-dot.fs-good, .fs-delta.fs-good, tr.fs-check.fs-good td) leaves every plain
    `class="num fs-good"` cell colorless. The regex requires the selector to START a
    line (^ in MULTILINE mode) so a scoped selector like `.fs-chip.fs-good{` -- where
    `.fs-good` appears mid-selector, not at line-start -- can't false-positive the
    match. Presence-of-class is not enough."""
    html = render_report_html(_fs_spec(_is_model()))
    assert '<td class="num fs-bad">' in html or '<td class="num fs-good">' in html
    style_block = html.split("<style>", 1)[1].split("</style>", 1)[0]
    assert re.search(r"^\s*\.fs-good\s*\{", style_block, re.MULTILINE)
    assert re.search(r"^\s*\.fs-bad\s*\{", style_block, re.MULTILINE)


def test_fs_trend_chart_grid_track_is_wide_enough_for_the_560px_floor():
    """EYEBALL-GATE FIX (F1): the mid-fold grid put the trend chart in the NARROW track
    and the variance quad in the WIDE one, even though the trend card is emitted FIRST
    and CSS declared 1.5fr for the first track -- because every .fs-quad cell carries
    white-space:nowrap (both the generic td.num,th.num rule and the first-column fix),
    making the quad table's min-content width (~587px unwrapped) exceed its "fair share"
    of the 1.5fr:1fr split. A plain fr track's minimum size defaults to the item's
    content size unless overridden, so the un-shrinkable quad ate space FROM the trend
    track regardless of the declared ratio. Fix: minmax(0, Nfr) tracks (overrides the
    automatic per-item minimum) + a much larger trend weight. This test proves the
    weights alone would satisfy the mock's >=560px trend width at the .report's actual
    content budget; a live screenshot confirms the min-content bug itself is defused."""
    html = render_report_html(_fs_spec(_is_model()))
    m = re.search(r"\.fs-mid\s*\{[^}]*grid-template-columns:\s*([^;]+);", html)
    assert m, "no .fs-mid grid-template-columns rule found"
    weights = [float(w) for w in re.findall(r"minmax\(0,\s*([\d.]+)fr\)", m.group(1))]
    assert len(weights) == 2, f"expected two minmax(0, Nfr) tracks, got: {m.group(1)!r}"
    trend_weight, quad_weight = weights
    assert trend_weight > quad_weight  # trend (first grid child) gets the WIDE slot
    # .report{max-width:840px; padding:48px 32px} minus the .fs-mid gap:18px
    avail_px = 840 - 2 * 32 - 18
    trend_px = avail_px * trend_weight / (trend_weight + quad_weight)
    assert trend_px >= 560, f"trend track would render at {trend_px:.0f}px, need >=560"


def test_fs_quad_table_is_scroll_wrapped_so_it_cannot_force_the_grid_track_wide():
    """Companion to the above: minmax(0, Nfr) lets the quad's track shrink below its
    unwrapped content width, so the quad table itself must tolerate that -- wrapped in
    .fs-scroll (the same overflow-x:auto pattern the statement table already uses) so a
    narrow column scrolls horizontally instead of visually breaking."""
    html = render_report_html(_fs_spec(_is_model()))
    assert '<div class="fs-scroll"><table class="fs-quad' in html


def test_fs_income_statement_is_two_step_interleaved():
    """EYEBALL-GATE FIX (F2, design rule #6): a two-step statement interleaves formula
    rows between sections -- Revenue -> Total Revenue -> COGS -> Total COGS -> Gross
    Profit -> OpEx -> Total OpEx -> Operating Income -> Other Income -> Total Other
    Income -> Other Expense -> Total Other Expense -> Net Income. NOT
    statement_builder's internal section-KEY grouping order (Revenue, Other Income,
    COGS, OpEx, Other Expense -- the SuiteQL/model grouping, an unrelated concern) with
    all three formula/net rows stacked at the very end."""
    html = render_report_html(_fs_spec(_is_model()))
    stmt_html = html.split('<table class="fs-stmt', 1)[1]

    anchors = [
        "▾</span> Revenue</label>",
        ">Total Revenue<",
        "▾</span> Cost of Goods Sold</label>",
        ">Total Cost of Goods Sold<",
        ">Gross Profit<",
        "▾</span> Operating Expense</label>",
        ">Total Operating Expense<",
        ">Operating Income<",
        "▾</span> Other Income</label>",
        ">Total Other Income<",
        "▾</span> Other Expense</label>",
        ">Total Other Expense<",
        ">Net Income<",
    ]
    positions = [stmt_html.index(a) for a in anchors]
    assert positions == sorted(positions), list(zip(anchors, positions, strict=True))


def test_fs_income_statement_two_step_interleave_holds_in_degraded_variant():
    """Same sequence requirement with no compare data (has_prior=False) -- row PRESENCE/
    ORDER must not depend on which optional columns are shown."""
    model = build_statement_model(fx.income_statement_section(), fx.income_statement_payloads_missing_compare())
    html = render_report_html(_fs_spec(model))
    stmt_html = html.split('<table class="fs-stmt', 1)[1]

    anchors = [
        "▾</span> Revenue</label>",
        ">Total Revenue<",
        "▾</span> Cost of Goods Sold</label>",
        ">Total Cost of Goods Sold<",
        ">Gross Profit<",
        "▾</span> Operating Expense</label>",
        ">Total Operating Expense<",
        ">Operating Income<",
        "▾</span> Other Income</label>",
        ">Total Other Income<",
        "▾</span> Other Expense</label>",
        ">Total Other Expense<",
        ">Net Income<",
    ]
    positions = [stmt_html.index(a) for a in anchors]
    assert positions == sorted(positions), list(zip(anchors, positions, strict=True))


def test_fs_balance_sheet_section_order_unchanged_by_two_step_interleave():
    """Confirmed-good at the eyeball gate: BS/TB sections are already sequential and
    must NOT be touched by the income_statement-only interleave logic."""
    model = build_statement_model(fx.balance_sheet_section(), fx.balance_sheet_payloads())
    html = render_report_html(_fs_spec(model, title="Balance Sheet"))
    stmt_html = html.split('<table class="fs-stmt', 1)[1]
    positions = [
        stmt_html.index(a)
        for a in ("▾</span> Assets</label>", "▾</span> Liabilities</label>", "▾</span> Equity</label>")
    ]
    assert positions == sorted(positions)


def test_fs_percent_integrity_canary_full_render_does_not_raise():
    """If a literal % anywhere in the financial-statement CSS were left un-doubled, the
    %-format call in render_report_html would raise at render time."""
    html = render_report_html(_fs_spec(_is_model()))
    assert "<style>" in html
    assert "fs-stmt" in html


def test_fs_balance_sheet_renders_checks_row_and_in_balance_chip():
    model = build_statement_model(fx.balance_sheet_section(), fx.balance_sheet_payloads())
    html = render_report_html(_fs_spec(model, title="Balance Sheet"))
    assert 'class="fs-check fs-good"' in html
    assert "Assets = Liabilities + Equity" in html
    assert 'class="fs-chip fs-good"' in html


def test_fs_balance_sheet_unbalanced_renders_bad_tone_check():
    model = build_statement_model(fx.balance_sheet_section(), fx.balance_sheet_unbalanced_payloads())
    html = render_report_html(_fs_spec(model, title="Balance Sheet"))
    assert 'class="fs-check fs-bad"' in html
    assert 'class="fs-chip fs-bad"' in html
    assert "off by" in html


def test_fs_trial_balance_renders_checks_row_and_chip():
    model = build_statement_model(fx.trial_balance_section(), fx.trial_balance_payloads())
    html = render_report_html(_fs_spec(model, title="Trial Balance"))
    assert 'class="fs-check fs-good"' in html
    assert "Debits = Credits" in html


def test_fs_degraded_model_no_compares_omits_delta_columns():
    model = build_statement_model(fx.income_statement_section(), fx.income_statement_payloads_missing_compare())
    assert model["prior_period"] is None  # sanity on the fixture contract
    html = render_report_html(_fs_spec(model))
    # "class=\"fs-delta" (not the bare substring) -- the CSS *rule definition* for
    # .fs-delta always ships in <style> regardless of use; only its usage in the body
    # is what the degraded model must omit.
    assert 'class="fs-delta' not in html  # no KPI MoM/YoY deltas anywhere
    assert "May 2026" not in html  # no prior-period column
    assert "$13,500,000" in html  # current-period figures still render


def test_fs_watch_items_render_with_model_driven_tone_dots():
    model = _is_model()
    html = render_report_html(_fs_spec(model))
    assert model["watch"], "fixture is expected to produce at least one watch item"
    for item in model["watch"]:
        assert escape(item["text"]) in html
        assert f'class="fs-dot fs-{item["tone"] if item["tone"] in ("good", "bad") else "warn"}"' in html


def test_fs_highlights_render_as_list_items():
    model = _is_model()
    html = render_report_html(_fs_spec(model))
    assert model["highlights"], "fixture is expected to produce at least one highlight"
    assert 'class="fs-hl"' in html
    for text in model["highlights"]:
        assert f"<li>{escape(text)}</li>" in html


def test_fs_narrative_renders_paragraphs():
    model = _is_model()
    html = render_report_html(_fs_spec(model))
    for text in model["narrative"]:
        assert f"<p>{escape(text)}</p>" in html


def test_fs_trend_chart_point_has_exact_value_title_tooltip():
    model = _is_model()
    html = render_report_html(_fs_spec(model))
    assert "<circle" in html
    assert "<title>Jun 2026 — Revenue: $13,500,000</title>" in html


def test_fs_css_only_included_when_statement_section_present():
    """The financial-statement stylesheet block is additive and conditional -- a report
    with no financial_statement section must not ship the extra CSS bytes at all (this is
    what makes the byte-stability guarantee below possible for a shared %-formatted _CSS)."""
    plain_html = render_report_html(_slice_d_spec())
    assert "fs-stmt" not in plain_html
    fs_html = render_report_html(_fs_spec(_is_model()))
    assert "fs-stmt" in fs_html


def test_fs_print_css_present_and_forces_full_expansion():
    html = render_report_html(_fs_spec(_is_model()))
    assert "@media print" in html
    printed = html.split("@media print", 1)[1]
    assert "fs-acct" in printed
    assert "!important" in printed
    assert "fs-sec-cb" in printed


def test_non_statement_spec_byte_stable_after_financial_statement_renderer_added():
    """Codifies the brief's byte-stability requirement: a spec with no financial_statement
    section must render BYTE-IDENTICALLY to what it rendered before this renderer existed.
    Pinned via SHA-256 (captured against the pre-Task-3 report_html.py) rather than a giant
    inline literal -- equally exact, far more maintainable."""
    import hashlib

    html = render_report_html(_slice_d_spec())
    assert (
        hashlib.sha256(html.encode()).hexdigest() == "6205e74d16941ef5d2c6fcb7e5b148867da7ff4e7075102dd35fefee1aa40661"
    )
