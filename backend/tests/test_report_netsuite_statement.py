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
        "14": _sec("Net Other Income", 612_000),
        "15": _sec("Net Income", 1_600_000),
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


# --- Income Statement: falls through to the meaty level-1 sections ---
def test_income_statement_curated_statement_includes_margin_lines():
    secs = _spec_of(_income_stmt_reportdata())["sections"]
    table = next(s for s in secs if s["type"] == "table")
    labels = _labels(table)
    assert table.get("curation") == "statement"
    assert "Financial Row" not in labels
    assert len(labels) <= 8
    # a board-ready P&L shows the margin story, not just the net skeleton
    assert "Income" in labels
    assert "Gross Profit" in labels
    assert "Net Income" in labels
    assert not any("40001" in x or "50000" in x for x in labels)  # accounts excluded
