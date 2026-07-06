"""Real-NetSuite statement curation (DoD-gap fix).

Live verification found the curated statement broke on REAL ns_runReport statements
(CF/P&L/BS), whose reportData differs from the synthetic fixtures: NO indentLevel (every
line level-0), section/total rows marked by a non-null ``label`` (name in ``value``),
UNNAMED detail lines, and an unnamed ``value:null`` grand-total. These feed the fixtures
through the real flatten (report_data_to_capped_table) so line_meta carries the derived
is_section/named/is_leaf/parent-level signals, then assert the curated statement is
coherent, the junk row is gone, and the driver chart charts real leaf accounts.
"""

from __future__ import annotations

from app.services.chat.tool_call_results import report_data_to_capped_table
from app.services.report.report_service import assemble_spec


def _sec(value, amt, parent=None):
    """A NetSuite section/subtotal/total row: label='Financial Row', name in ``value``."""
    return {"label": "Financial Row", "value": value, "parent": parent, "isDetailLine": False,
            "summaryLineValues": [{"Amount": amt}]}  # fmt: skip


def _acct(value, amt):
    """A named leaf account (isDetailLine=false); paired with its detail marker below."""
    return {"label": None, "value": value, "parent": "finandim_srawfullname", "isDetailLine": False,
            "summaryLineValues": [{"Amount": amt}]}  # fmt: skip


def _det(amt):
    """The unnamed isDetailLine=true marker that follows each leaf account."""
    return {"label": None, "value": None, "parent": "finandim_srawvalidname", "isDetailLine": True,
            "detailLineValues": [{"Amount": amt}]}  # fmt: skip


def _junk(amt):
    """Entry 0: the unnamed grand-total whose only name is the placeholder label."""
    return {"label": "Financial Row", "value": None, "parent": None, "isDetailLine": False,
            "summaryLineValues": [{"Amount": amt}]}  # fmt: skip


def _cash_flow_reportdata() -> dict:
    # Header + its "Total X" carry the SAME amount (dedupe target); a level-1 section
    # (Net Income) sits inside Operating so the dedupe must be per-level.
    return {
        "0": _junk(30_000_000),
        "1": _sec("Operating Activities", -1_600_000),
        "2": _sec("Net Income", 1_600_000, parent="finandim_srawfullname"),
        "3": _acct("12000 - Inventory", -9_800_000),
        "4": _det(-9_800_000),
        "5": _acct("20000 - Accounts Payable", 8_700_000),
        "6": _det(8_700_000),
        "7": _acct("11010 - Intercompany Receivables", -6_000_000),
        "8": _det(-6_000_000),
        "9": _sec("Total Operating Activities", -1_600_000),
        "10": _sec("Investing Activities", 780_000),
        "11": _acct("17005 - R&D", -868_000),
        "12": _det(-868_000),
        "13": _sec("Total Investing Activities", 780_000),
        "14": _sec("Financing Activities", -1_100_000),
        "15": _acct("26000 - Note Payable", -978_000),
        "16": _det(-978_000),
        "17": _sec("Total Financing Activities", -1_100_000),
        "18": _sec("Net Change in Cash", -1_920_000),
        "19": _sec("Cash at Beginning of Period", 18_300_000),
        "20": _sec("Effect of Exchange Rate", 945_000),
        "21": _sec("Cash at End of Period", 17_280_000),
    }


def _income_stmt_reportdata() -> dict:
    # P&L: the meaty sections (Income/COGS/Gross Profit/Expense) are level-1; level-0 is only
    # the 3 net lines (+ redundant header/total pairs). The curation must fall through to
    # level-1 so a board reads Revenue/COGS/Gross Profit, not just the net skeleton.
    return {
        "0": _junk(4_800_000),
        "1": _sec("Ordinary Income/Expense", 990_000),
        "2": _sec("Income", 122_000_000, parent="finandim_srawfullname"),
        "3": _acct("40001 - Sales", 120_000_000),
        "4": _det(120_000_000),
        "5": _sec("Cost Of Sales", -85_000_000, parent="finandim_srawfullname"),
        "6": _acct("50000 - COGS", -72_000_000),
        "7": _det(-72_000_000),
        "8": _sec("Gross Profit", 37_000_000, parent="finandim_srawfullname"),
        "9": _sec("Expense", -36_000_000, parent="finandim_srawfullname"),
        "10": _acct("66000 - R&D", -9_000_000),
        "11": _det(-9_000_000),
        "12": _sec("Net Ordinary Income", 990_000),
        "13": _sec("Other Income and Expenses", 612_000),
        "14": _sec("Other Expense", 612_000, parent="finandim_srawfullname"),
        "15": _acct("90003 - Exchange Gain/Loss", 612_000),
        "16": _det(612_000),
        "17": _sec("Net Other Income", 612_000),
        "18": _sec("Net Income", 1_600_000),
    }


def _spec_of(reportdata: dict) -> dict:
    columns, rows, line_meta, row_count, truncated = report_data_to_capped_table(reportdata)
    payload = {
        "columns": columns,
        "rows": rows,
        "row_count": row_count,
        "truncated": truncated,
        "currency_columns": ["amount"],
        "line_meta": line_meta,
    }
    return assemble_spec("Statement", [{"type": "table", "result_id": "s"}], lambda rid: payload)


def _labels(section: dict) -> list[str]:
    return [str(r[0]) for r in section["rows"]]


# --- Cash Flow: coherent level-0 section flows, no junk, no accounts, leaf-driver chart ---
def test_cash_flow_curated_statement_is_coherent_sections():
    secs = _spec_of(_cash_flow_reportdata())["sections"]
    table = next(s for s in secs if s["type"] == "table")
    labels = _labels(table)
    assert table.get("curation") == "statement"
    assert len(labels) <= 8
    assert "Financial Row" not in labels  # the junk grand-total is gone (fix #1)
    # all three sections survive — Investing/Financing are no longer dropped (fix #2)
    assert any("Operating" in x for x in labels)
    assert any("Investing" in x for x in labels)
    assert any("Financing" in x for x in labels)
    assert "Net Change in Cash" in labels and "Cash at End of Period" in labels
    # detail accounts do NOT pollute the statement
    assert not any("12000" in x or "20000" in x for x in labels)


def test_cash_flow_callouts_are_the_conclusions():
    secs = _spec_of(_cash_flow_reportdata())["sections"]
    callouts = [s for s in secs if s["type"] == "metric_headline"]
    assert 1 <= len(callouts) <= 4
    assert any(c["label"] == "Cash at End of Period" for c in callouts)
    assert all(c["label"] != "Financial Row" for c in callouts)


def test_cash_flow_driver_chart_is_leaf_accounts_not_sections():
    secs = _spec_of(_cash_flow_reportdata())["sections"]
    chart = next((s for s in secs if s["type"] == "chart"), None)
    assert chart is not None, "a statement with real leaf accounts should chart its drivers (fix #3)"
    assert chart["chart_type"] == "bar"
    svg = chart["svg"]
    assert "12000 - Inventory" in svg or "20000" in svg  # real leaf movers
    # NO section/total/grand-total bars (no double-count / bar-soup)
    for bad in ("Total Operating", "Total Investing", "Total Financing", "Net Change", "Cash at End", "Financial Row"):
        assert bad not in svg


# --- Income Statement: coherent net structure (margins live in the driver chart) ---
def test_income_statement_shows_coherent_net_structure():
    secs = _spec_of(_income_stmt_reportdata())["sections"]
    table = next(s for s in secs if s["type"] == "table")
    labels = _labels(table)
    assert table.get("curation") == "statement"
    assert "Financial Row" not in labels
    assert len(labels) <= 8
    # NetSuite names a P&L section total ("Net Ordinary Income") differently from its header
    # ("Ordinary Income/Expense"), so the safe label-nest fold does NOT merge them and the
    # coherent net structure survives (the margin/account detail is in the driver chart).
    assert "Net Ordinary Income" in labels
    assert "Net Income" in labels
    assert not any("40001" in x or "50000" in x for x in labels)  # detail accounts excluded


def test_junk_grand_total_never_becomes_a_chart_leaf():
    # Regression (T2 re-gate): the unnamed grand-total (label='Financial Row', value=None) is a
    # SECTION, not an account — it must never be is_leaf even when followed by a detail marker.
    from app.services.chat.tool_call_results import _extract_report_data_as_table

    _c, _r, meta = _extract_report_data_as_table(
        {
            "0": {
                "label": "Financial Row",
                "value": None,
                "parent": None,
                "isDetailLine": False,
                "summaryLineValues": [{"Amount": 30000}],
            },  # fmt: skip
            "1": {
                "label": None,
                "value": None,
                "parent": "x",
                "isDetailLine": True,
                "detailLineValues": [{"Amount": 30000}],
            },  # fmt: skip
        }
    )
    assert meta[0]["is_section"] is True and meta[0]["is_leaf"] is False


def test_header_plus_total_only_falls_to_floor_not_one_line():
    # Regression (T2 re-gate): a statement that is only a section header + its own total folds
    # to a single row; the curated-statement path must DECLINE (fall to the floor), never emit
    # a one-line "curated statement".
    rd = {
        "0": _sec("Operating Activities", -1_600_000),
        "1": _acct("11000 - Accounts Receivable", -1_600_000),
        "2": _det(-1_600_000),
        "3": _sec("Total Operating Activities", -1_600_000),
    }
    table = next(s for s in _spec_of(rd)["sections"] if s["type"] == "table")
    # a 1-row "curated statement" is the collapse bug: either the floor kicked in (curation is
    # no longer "statement") or the statement kept the minimum >= 2 rows.
    assert not (table.get("curation") == "statement" and len(table["rows"]) < 2)


# --- Safety of the section selector (regression for the T2-gate majors) ---
from app.services.report.report_service import _select_statement_sections  # noqa: E402


def _p(level, index, label, amount):
    return (level, index, [label, amount], amount)


def _sel_labels(picked):
    return [p[2][0] for p in _select_statement_sections(picked)]


def test_distinct_zero_sections_both_survive():
    # Two DISTINCT $0 sections must NOT be merged by the dedupe — never drop a figure
    # (the exact "two $0 balance-sheet lines" hazard the flatten docstring warns about).
    labels = _sel_labels(
        [_p(0, 0, "Unbilled Receivable", 0), _p(0, 1, "Other Receivable", 0), _p(0, 2, "Net Income", 500)]
    )
    assert "Unbilled Receivable" in labels and "Other Receivable" in labels
    assert "Net Income" in labels


def test_coincidental_equal_amount_sections_both_survive():
    # Two unrelated sections that merely tie in amount (no label containment) must both survive.
    labels = _sel_labels([_p(0, 0, "Operating", 1000), _p(0, 1, "Investing", 1000), _p(0, 2, "Net Change", -50)])
    assert "Operating" in labels and "Investing" in labels


def test_header_and_its_total_collapse_to_one():
    # A header and its own subtotal (label containment + equal amount) collapse to the subtotal.
    labels = _sel_labels(
        [
            _p(0, 0, "Operating Activities", -1600),
            _p(0, 1, "Total Operating Activities", -1600),
            _p(0, 2, "Cash at End", 17000),
        ]
    )
    assert labels.count("Operating Activities") == 0  # the header folded into its total
    assert "Total Operating Activities" in labels and "Cash at End" in labels


def test_true_conclusion_at_deeper_level_always_survives():
    # Selection stops at level 0 (reaches the richness floor) but the true close is level 1 —
    # it must still appear (the T2 gate r5 invariant the old trim guaranteed).
    labels = _sel_labels(
        [_p(0, 0, "A", 1), _p(0, 1, "B", 2), _p(0, 2, "C", 3), _p(0, 3, "D", 4), _p(1, 4, "Net Income", 5)]
    )
    assert "Net Income" in labels


def test_over_max_keeps_shallowest_and_conclusion():
    picked = [_p(0, i, f"S{i}", i + 1) for i in range(9)] + [_p(0, 9, "Net Change", 100)]
    out = _select_statement_sections(picked)
    labels = [p[2][0] for p in out]
    assert len(out) <= 8
    assert "Net Change" in labels  # the conclusion is never evicted
    assert "S0" in labels  # shallowest/earliest kept, not head+tail-dropped
