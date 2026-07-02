"""Phase 3 — curated statement + key-figure callouts (composition).

A statement-shaped result (reportData → line_meta present) must render as a genuine
summary — the product-owner-confirmed shape "callouts + statement": 3-5 named
metric_headline cards for the marquee figures AND a compact ≤8-line curated statement
of the section subtotals. Detail lines, blank-label continuation rows, and amount-less
placeholder rows drop out STRUCTURALLY (via line_meta + amounts — never name filtering,
no prompt pollution). Everything without line_meta keeps the general top-K floor.
"""

from __future__ import annotations

import json

from app.services.report.report_service import (
    _REPORT_TABLE_TOP_K,
    _STATEMENT_CALLOUT_MAX,
    _STATEMENT_TABLE_MAX,
    _resolve_data_section,
    assemble_spec,
)


def _meta(is_summary: bool, level: int = 0) -> dict:
    return {"is_summary": is_summary, "level": level}


def _statement_payload() -> dict:
    """A cash-flow-like flattened reportData payload: placeholder (no amount), section
    summaries, detail lines, a blank continuation row, and trailing marquee summaries."""
    rows = [
        ["Financial Row", None],  # placeholder — no amount → dropped structurally
        ["Net Income", 5_200_000],
        ["11000 - Accounts Receivable", -1_400_000],
        ["12000 - Inventory", -600_000],
        ["", -600_000],  # blank continuation row → dropped from the statement
        ["Operating Activities", 8_100_000],
        ["14000 - Fixed Assets", -2_300_000],
        ["Investing Activities", -2_300_000],
        ["Financing Activities", -1_050_000],
        ["Net Change in Cash", 4_750_000],
        ["Cash at End of Period", 11_500_000],
    ]
    meta = [
        _meta(True),  # placeholder is a section header (summary) but has NO amount
        _meta(True, 1),  # Net Income — deeper-level summary
        _meta(False, 2),
        _meta(False, 2),
        _meta(False, 2),
        _meta(True, 0),  # Operating Activities
        _meta(False, 1),
        _meta(True, 0),  # Investing Activities
        _meta(True, 0),  # Financing Activities
        _meta(True, 0),  # Net Change in Cash
        _meta(True, 0),  # Cash at End of Period
    ]
    # A REAL un-truncated payload carries ALL its flattened rows (row_count == len(rows),
    # truncated False) — the statement CURATION is what trims, not the payload.
    return {
        "kind": "table",
        "columns": ["account", "amount"],
        "rows": rows,
        "row_count": len(rows),
        "truncated": False,
        "currency_columns": ["amount"],
        "line_meta": meta,
    }


def _resolve(payload: dict) -> dict:
    return _resolve_data_section({"type": "table", "result_id": "r1"}, lambda rid: payload)


# ---------------------------------------------------------------------------
# Statement curation: labeled summary lines with amounts only.
# ---------------------------------------------------------------------------
def test_statement_curates_to_named_summary_lines_only():
    out = _resolve(_statement_payload())
    assert out["type"] == "table"
    labels = [r[0] for r in out["rows"]]
    # summaries with amounts, statement order preserved
    assert labels == [
        "Net Income",
        "Operating Activities",
        "Investing Activities",
        "Financing Activities",
        "Net Change in Cash",
        "Cash at End of Period",
    ]
    assert "" not in labels  # blank continuation rows gone
    assert "Financial Row" not in labels  # amount-less placeholder gone (structural)
    assert "11000 - Accounts Receivable" not in labels  # detail lines gone
    assert out["curation"] == "statement"
    assert out["row_count"] == 11  # true total preserved
    assert out["truncated"] is True  # derived: 6 curated lines < 11 source rows
    assert out["currency_columns"] == ["amount"]  # formatting tag survives


def test_statement_callouts_are_trailing_marquee_figures_formatted():
    out = _resolve(_statement_payload())
    callouts = out["statement_callouts"]
    assert 1 <= len(callouts) <= _STATEMENT_CALLOUT_MAX
    assert [c["label"] for c in callouts] == [
        "Investing Activities",
        "Financing Activities",
        "Net Change in Cash",
        "Cash at End of Period",
    ]  # the LAST 4 curated lines — a statement builds to its conclusions
    assert all(c["type"] == "metric_headline" for c in callouts)
    # accounting-formatted: thousands separators, negatives in parentheses
    by_label = {c["label"]: c["value"] for c in callouts}
    assert by_label["Cash at End of Period"] == "11,500,000.00"
    assert by_label["Investing Activities"] == "(2,300,000.00)"


def test_statement_caps_at_max_preferring_shallow_levels():
    # 5 top-level sections + 5 deeper subtotals in the middle + a top-level closing line
    # (a realistic statement ENDS on a top-level conclusion). The cap keeps the shallow
    # (most aggregate) lines — including the trailing conclusion — trimming the deep ones.
    rows, meta = [], []
    for i in range(5):
        rows.append([f"Section {i}", (i + 1) * 1000])
        meta.append(_meta(True, 0))
    for i in range(5):
        rows.append([f"Subtotal {i}", (i + 1) * 10])
        meta.append(_meta(True, 1))
    rows.append(["Grand Total", 99_000])
    meta.append(_meta(True, 0))
    payload = {
        "columns": ["account", "amount"],
        "rows": rows,
        "row_count": len(rows),
        "currency_columns": ["amount"],
        "line_meta": meta,
    }
    out = _resolve(payload)
    labels = [r[0] for r in out["rows"]]
    assert len(labels) <= _STATEMENT_TABLE_MAX
    assert labels == [f"Section {i}" for i in range(5)] + ["Grand Total"]  # shallow kept, deep trimmed


# ---------------------------------------------------------------------------
# Fallbacks: anything not statement-shaped keeps the general top-K floor.
# ---------------------------------------------------------------------------
def _generic_rows(n: int) -> list:
    return [[f"P{i:03d}", i * 100] for i in range(n)]


def test_no_line_meta_keeps_top_k_floor():
    payload = {"columns": ["Period", "Revenue"], "rows": _generic_rows(30), "row_count": 30}
    out = _resolve(payload)
    assert len(out["rows"]) == _REPORT_TABLE_TOP_K
    assert out["rows"][0][0] == "P000"  # source order, first-K
    assert "statement_callouts" not in out
    assert "curation" not in out


def test_all_detail_line_meta_falls_back_to_top_k():
    rows = _generic_rows(30)
    payload = {
        "columns": ["account", "amount"],
        "rows": rows,
        "row_count": 30,
        "line_meta": [_meta(False) for _ in rows],
    }
    out = _resolve(payload)
    assert len(out["rows"]) == _REPORT_TABLE_TOP_K
    assert "statement_callouts" not in out


def test_single_summary_line_falls_back_to_top_k():
    rows = _generic_rows(20)
    meta = [_meta(False) for _ in rows]
    meta[3] = _meta(True)
    payload = {"columns": ["account", "amount"], "rows": rows, "row_count": 20, "line_meta": meta}
    out = _resolve(payload)
    assert len(out["rows"]) == _REPORT_TABLE_TOP_K  # one summary ≠ a statement
    assert "statement_callouts" not in out


def test_misaligned_line_meta_falls_back_to_top_k():
    rows = _generic_rows(20)
    payload = {
        "columns": ["account", "amount"],
        "rows": rows,
        "row_count": 20,
        "line_meta": [_meta(True)] * 3,  # wrong length → unsafe to key curation off it
    }
    out = _resolve(payload)
    assert len(out["rows"]) == _REPORT_TABLE_TOP_K
    assert "statement_callouts" not in out


def test_select_projection_disables_statement_treatment():
    # A model-projected `select` re-indexes columns; the conservative rule is to keep the
    # plain top-K floor there rather than risk curating off the wrong column.
    payload = _statement_payload()
    out = _resolve_data_section({"type": "table", "result_id": "r1", "select": ["account"]}, lambda rid: payload)
    assert "statement_callouts" not in out
    assert "curation" not in out


# ---------------------------------------------------------------------------
# assemble_spec: callouts emitted BEFORE the statement table; internal key stripped.
# ---------------------------------------------------------------------------
def test_assemble_spec_emits_callouts_before_statement_table():
    payload = _statement_payload()
    spec = assemble_spec(
        "Cash Flow Review",
        [
            {"type": "narrative", "markdown": "Liquidity improved."},
            {"type": "table", "result_id": "r1"},
        ],
        lambda rid: payload,
    )
    types = [s["type"] for s in spec["sections"]]
    first_headline = types.index("metric_headline")
    table_idx = types.index("table")
    assert first_headline < table_idx  # cards render above the statement
    assert types.count("metric_headline") == 4
    # the internal hand-off key never leaks into the frozen spec
    assert all("statement_callouts" not in s for s in spec["sections"])
    # the guaranteed-chart floor still applies to the curated statement
    assert "chart" in types


def test_statement_table_html_shows_curated_note_and_no_blanks():
    from app.services.report.report_html import render_report_html

    payload = _statement_payload()
    spec = assemble_spec("CF", [{"type": "table", "result_id": "r1"}], lambda rid: payload)
    html = render_report_html(spec)
    assert "Curated statement" in html  # not the misleading "Showing first N of M"
    assert "Showing first" not in html
    assert "5,200,000.00" in html  # Net Income accounting-formatted in the table
    assert "(2,300,000.00)" in html  # negatives in parentheses
    assert "11,500,000.00" in html  # marquee callout value present


# ---------------------------------------------------------------------------
# End-to-end shape: raw reportData → extract → resolve → assemble → html.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# TRUST BOUNDARY (T2 gate r2 — BLOCKER): heading/divider sections pass through as the
# LLM's own dict, so a model-authored `statement_callouts` key must NEVER be honored —
# forged numbers, unescaped svg, or a crash would otherwise reach the frozen report.
# ---------------------------------------------------------------------------
def test_forged_callouts_on_heading_never_reach_the_report():
    from app.services.report.report_html import render_report_html

    payload = {"columns": ["Period", "Revenue"], "rows": [["Q1", 100], ["Q2", 200]], "row_count": 2}
    spec = assemble_spec(
        "R",
        [
            {
                "type": "heading",
                "text": "Q2",
                "statement_callouts": [
                    {"type": "metric_headline", "label": "Net Income", "value": "5.3M approx"},
                    {"type": "chart", "svg": "<script>alert(1)</script>", "chart_type": "bar"},
                ],
            },
            {"type": "table", "result_id": "r1"},
        ],
        lambda rid: payload,
    )
    html = render_report_html(spec)
    assert "5.3M approx" not in html  # an LLM-invented number never renders (no-LLM-numbers)
    assert "<script>" not in html  # forged svg never reaches the trusted-chart sink
    assert all(s["type"] != "metric_headline" for s in spec["sections"])
    # the heading passthrough is SANITIZED — no LLM extra keys survive into the spec
    heading = next(s for s in spec["sections"] if s["type"] == "heading")
    assert set(heading) <= {"type", "level", "text"}


def test_forged_callouts_string_on_divider_does_not_crash_render():
    from app.services.report.report_html import render_report_html

    payload = {"columns": ["Period", "Revenue"], "rows": [["Q1", 100], ["Q2", 200]], "row_count": 2}
    spec = assemble_spec(
        "R",
        [{"type": "divider", "statement_callouts": "netincome"}, {"type": "table", "result_id": "r1"}],
        lambda rid: payload,
    )
    html = render_report_html(spec)  # must not raise (extend("netincome") would iterate chars)
    assert "netincome" not in html
    divider = next(s for s in spec["sections"] if s["type"] == "divider")
    assert set(divider) == {"type"}


# ---------------------------------------------------------------------------
# Tail-cut honesty (T2 gate r2 — MAJOR): a payload whose rows were truncated BEFORE
# curation (storage cap / NetSuite-side cut) may be missing the statement's concluding
# lines — claiming "curated statement" over a partial statement is dishonest. Fall back
# to the top-K floor, whose note discloses the truncation.
# ---------------------------------------------------------------------------
def test_truncated_payload_gets_top_k_floor_not_statement_claim():
    payload = _statement_payload()
    payload["truncated"] = True  # rows were tail-cut upstream of curation
    out = _resolve(payload)
    assert "curation" not in out
    assert "statement_callouts" not in out
    assert len(out["rows"]) <= _REPORT_TABLE_TOP_K
    assert out["truncated"] is True  # the floor's honest disclosure applies


# ---------------------------------------------------------------------------
# Flat-statement overflow (T2 gate r2 — MAJOR): 10+ same-level summaries (the realistic
# no-indent-key reportData) must keep the statement's CONCLUSIONS — head + tail, never
# just the first 8 (which cut Net Change / Ending Cash from table AND callouts).
# ---------------------------------------------------------------------------
def test_lone_shallow_summary_does_not_collapse_statement_to_one_line():
    # 1 lone level-0 summary (a grand-total wrapper) + 10 level-1 subtotals ending in the
    # marquee conclusions: the threshold trim used to pick the 1-row level-0 subset
    # (≤8 fits!), rendering a one-line "curated statement" and losing Net Change /
    # Ending Cash from BOTH table and callouts (T2 gate r3: major). A degenerately small
    # threshold subset must fall through to head+tail over ALL qualifying lines.
    rows = [["Cash Flow", 4_750_000]]
    meta = [_meta(True, 0)]
    for i in range(8):
        rows.append([f"Subtotal {i}", (i + 1) * 1000])
        meta.append(_meta(True, 1))
    rows += [["Net Change in Cash", 4_750_000], ["Cash at End of Period", 11_500_000]]
    meta += [_meta(True, 1), _meta(True, 1)]
    payload = {
        "columns": ["account", "amount"],
        "rows": rows,
        "row_count": len(rows),
        "truncated": False,
        "currency_columns": ["amount"],
        "line_meta": meta,
    }
    out = _resolve(payload)
    labels = [r[0] for r in out["rows"]]
    assert len(labels) == _STATEMENT_TABLE_MAX  # never a degenerate 1-line statement
    assert "Net Change in Cash" in labels and "Cash at End of Period" in labels
    assert [c["label"] for c in out["statement_callouts"]][-1] == "Cash at End of Period"


def test_shallow_subset_never_cuts_the_statements_conclusions():
    # 3 shallow (level-0) mid-statement lines + 9 deeper (level-1) lines whose TAIL holds
    # the conclusions. The threshold trim used to pick the 3-line level-0 subset (fits
    # 2..8) — cutting Net Change / Ending Cash from BOTH table and callouts (gate r5:
    # major). A threshold subset only qualifies if it CONTAINS the statement's last
    # qualifying line; otherwise head+tail over all qualifying lines.
    rows, meta = [], []
    for i in range(3):
        rows.append([f"Section {i}", (i + 1) * 1000])
        meta.append(_meta(True, 0))
    for i in range(7):
        rows.append([f"Subtotal {i}", (i + 1) * 10])
        meta.append(_meta(True, 1))
    rows += [["Net Change in Cash", 4_750_000], ["Cash at End of Period", 11_500_000]]
    meta += [_meta(True, 1), _meta(True, 1)]
    payload = {
        "columns": ["account", "amount"],
        "rows": rows,
        "row_count": len(rows),
        "truncated": False,
        "currency_columns": ["amount"],
        "line_meta": meta,
    }
    out = _resolve(payload)
    labels = [r[0] for r in out["rows"]]
    assert len(labels) <= _STATEMENT_TABLE_MAX
    assert "Net Change in Cash" in labels and "Cash at End of Period" in labels
    assert [c["label"] for c in out["statement_callouts"]][-1] == "Cash at End of Period"


def test_duplicate_statement_table_renders_callouts_once():
    # The auto-chart dedupes repeated tables via (result_id, select); the callout cards
    # must dedupe the same way — a composition repeating the statement table must not
    # stack two identical rows of marquee cards (T2 gate r3).
    payload = _statement_payload()
    spec = assemble_spec(
        "CF",
        [{"type": "table", "result_id": "r1"}, {"type": "table", "result_id": "r1"}],
        lambda rid: payload,
    )
    headlines = [s for s in spec["sections"] if s["type"] == "metric_headline"]
    assert len(headlines) == 4  # one set of callouts, not two


def test_flat_statement_overflow_keeps_opening_and_conclusions():
    rows = [[f"Section {i}", (i + 1) * 1000] for i in range(8)]
    rows += [["Net Change in Cash", 4_750_000], ["Cash at End of Period", 11_500_000]]
    meta = [_meta(True, 0)] * 10  # single level — the realistic no-indent case
    payload = {
        "columns": ["account", "amount"],
        "rows": rows,
        "row_count": 10,
        "currency_columns": ["amount"],
        "line_meta": meta,
    }
    out = _resolve(payload)
    labels = [r[0] for r in out["rows"]]
    assert len(labels) == _STATEMENT_TABLE_MAX
    assert "Section 0" in labels  # the opening lines survive…
    assert "Net Change in Cash" in labels and "Cash at End of Period" in labels  # …and the conclusions
    assert [c["label"] for c in out["statement_callouts"]][-1] == "Cash at End of Period"


# ---------------------------------------------------------------------------
# Honest flags + junk guards (gate r2 minors).
# ---------------------------------------------------------------------------
def test_statement_truncated_flag_derived_not_hardcoded():
    # ALL source rows qualify → nothing dropped → truncated must be False.
    rows = [[f"S{i}", (i + 1) * 10] for i in range(4)]
    payload = {
        "columns": ["account", "amount"],
        "rows": rows,
        "row_count": 4,
        "currency_columns": ["amount"],
        "line_meta": [_meta(True, 0)] * 4,
    }
    out = _resolve(payload)
    assert out["curation"] == "statement"
    assert out["truncated"] is False


def test_blank_string_amount_line_does_not_qualify():
    payload = {
        "columns": ["account", "amount"],
        "rows": [["Real", 100], ["Junk", "  "], ["Also Real", 200]],
        "row_count": 3,
        "currency_columns": ["amount"],
        "line_meta": [_meta(True, 0)] * 3,
    }
    out = _resolve(payload)
    labels = [r[0] for r in out["rows"]]
    assert labels == ["Real", "Also Real"]  # a whitespace amount is not a figure


def test_statement_note_coerces_numeric_string_row_count():
    from app.services.report.report_html import render_report_html

    payload = _statement_payload()
    payload["row_count"] = "11"  # some MCP shapes serialize counts as strings
    spec = assemble_spec("CF", [{"type": "table", "result_id": "r1"}], lambda rid: payload)
    html = render_report_html(spec)
    assert "from 11 source rows" in html  # the true total still disclosed


def test_curate_statement_level_parses_float_strings_like_producer():
    # meta levels may round-trip as "0.0"/"1.0" strings; the threshold trim must parse
    # them like the producer (int(float(...))), keeping the shallow lines (incl. the
    # trailing top-level conclusion). Were "0.0"/"1.0" unparseable, every level would
    # default to 0 and the 11-line set would head+tail instead — a different result.
    rows = [[f"Top {i}", (i + 1) * 100] for i in range(5)] + [[f"Deep {i}", i + 1] for i in range(5)]
    rows.append(["Ending Balance", 55_000])
    meta = [_meta(True, "0.0") for _ in range(5)] + [_meta(True, "1.0") for _ in range(5)]
    meta.append(_meta(True, "0.0"))
    payload = {
        "columns": ["account", "amount"],
        "rows": rows,
        "row_count": 11,
        "currency_columns": ["amount"],
        "line_meta": meta,
    }
    out = _resolve(payload)
    labels = [r[0] for r in out["rows"]]
    assert labels == [f"Top {i}" for i in range(5)] + ["Ending Balance"]


def test_reportdata_end_to_end_produces_summary_not_dump():
    from app.services.chat.tool_call_results import extract_result_payload
    from app.services.report.report_html import render_report_html

    report_data = {
        "0": {"label": "Financial Row", "isDetailLine": False, "summaryLineValues": [{}]},
        "1": {"label": "Net Income", "isDetailLine": False, "summaryLineValues": [{"Amount": 5200000}]},
        "2": {"label": "11000 - Accounts Receivable", "isDetailLine": True, "detailLineValues": [{"amount": -1400000}]},
        "3": {"label": "", "isDetailLine": True, "detailLineValues": [{"amount": -1400000}]},
        "4": {"label": "Operating Activities", "isDetailLine": False, "summaryLineValues": [{"Amount": 8100000}]},
        "5": {"label": "Cash at End of Period", "isDetailLine": False, "summaryLineValues": [{"Amount": 11500000}]},
    }
    payload = extract_result_payload("ext__abc__ns_runReport", {}, json.dumps({"reportData": report_data}))
    assert payload is not None and "line_meta" in payload
    spec = assemble_spec("Cash Flow", [{"type": "table", "result_id": "r1"}], lambda rid: payload)
    html = render_report_html(spec)
    table = next(s for s in spec["sections"] if s["type"] == "table")
    labels = [r[0] for r in table["rows"]]
    assert labels == ["Net Income", "Operating Activities", "Cash at End of Period"]
    assert "11000 - Accounts Receivable" not in html  # no detail dump
    headlines = [s for s in spec["sections"] if s["type"] == "metric_headline"]
    assert [h["label"] for h in headlines] == ["Net Income", "Operating Activities", "Cash at End of Period"]
