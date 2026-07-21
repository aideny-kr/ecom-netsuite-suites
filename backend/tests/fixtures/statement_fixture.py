"""Hand-computed fixtures for ``statement_builder.build_statement_model`` tests.

Every payload here mimics the ``extract_result_payload``-normalized shape
(``{"columns": [...], "rows": [[...], ...]}``) that a resolved ``netsuite_financial_report``
source produces — see ``app/mcp/tools/netsuite_financial_report.py`` (REPORT_TEMPLATES) for
the real column lists this mirrors:
  - income_statement / income_statement_trend: acctnumber, acctname, accttype, section, amount
    (income_statement_trend additionally has periodname, startdate)
  - balance_sheet: acctnumber, acctname, accttype, section, balance
  - trial_balance: acctnumber, acctname, accttype, total_debit, total_credit, net_amount (NO
    section column — trial_balance is a flat GL listing, not grouped by statement section)

All arithmetic below is hand-computed and cross-checked; every derived total the tests need is
ALSO exposed as an ``EXPECTED_*`` module constant (Decimal or the exact formatted string the
builder should produce), so tests assert against a hand-checked number — never a recomputation
of the code under test.

Section grouping matches ``build_period_filter``'s CASE mapping exactly:
  IS:  1-Revenue, 2-Other Income, 3-COGS, 4-Operating Expense, 5-Other Expense
  BS:  1-Assets, 2-Liabilities, 3-Equity
  TB:  no section column at all.
"""

from __future__ import annotations

from decimal import Decimal


def _payload(columns: list[str], row_dicts: list[dict], *, query: str = "") -> dict:
    """Build an ``extract_result_payload``-shaped success payload from compact row dicts."""
    money_cols = {"amount", "balance", "total_debit", "total_credit", "net_amount"}
    return {
        "kind": "table",
        "columns": columns,
        "rows": [[rd.get(c) for c in columns] for rd in row_dicts],
        "row_count": len(row_dicts),
        "truncated": False,
        "query": query,
        "limit": len(row_dicts),
        "currency_columns": [c for c in columns if c in money_cols],
    }


def _failed(error: str = "SuiteQL execution failed: timeout") -> dict:
    return {"success": False, "error": error}


# ---------------------------------------------------------------------------
# section dicts — mirror playbooks.build_playbook_recipe's financial_statement shape exactly
# ---------------------------------------------------------------------------


def income_statement_section(period: str = "Jun 2026") -> dict:
    return {
        "type": "financial_statement",
        "result_id": "r1",
        "statement": "income_statement",
        "period": period,
        "compare": {"prior": "r2", "yoy": "r3", "trend": "r4"},
    }


def balance_sheet_section(period: str = "Jun 2026") -> dict:
    return {
        "type": "financial_statement",
        "result_id": "r1",
        "statement": "balance_sheet",
        "period": period,
        "compare": {"prior": "r2"},
    }


def trial_balance_section(period: str = "Jun 2026") -> dict:
    return {
        "type": "financial_statement",
        "result_id": "r1",
        "statement": "trial_balance",
        "period": period,
        "compare": {"prior": "r2"},
    }


# ===========================================================================
# INCOME STATEMENT — realistic 30-account fixture (Framework-shaped magnitudes)
# ===========================================================================

_IS_COLUMNS = ["acctnumber", "acctname", "accttype", "section", "amount"]


def _is_row(number: str, name: str, accttype: str, section: str, amount) -> dict:
    return {"acctnumber": number, "acctname": name, "accttype": accttype, "section": section, "amount": amount}


# --- r1: Jun 2026 (current period) — 30 accounts across all 5 IS sections -------------
# 1-Revenue (6 accounts): 12,600,000 + 700,000 + 250,000 + 50,000 + 150,000 - 250,000
#   = 13,750,000 - 250,000 = 13,500,000
_IS_R1_REVENUE = [
    _is_row("4000", "Product Sales", "Income", "1-Revenue", Decimal("12600000")),
    _is_row("4010", "Subscription Revenue", "Income", "1-Revenue", Decimal("700000")),
    _is_row("4020", "Shipping Revenue", "Income", "1-Revenue", Decimal("250000")),
    _is_row("4030", "Gift Card Redemptions", "Income", "1-Revenue", Decimal("50000")),
    _is_row("4040", "Other Sales", "Income", "1-Revenue", Decimal("150000")),
    _is_row("4900", "Sales Returns and Allowances", "Income", "1-Revenue", Decimal("-250000")),
]
EXPECTED_REVENUE = Decimal("13500000")

# 2-Other Income (3 accounts): 12,000 + 5,000 + 3,000 = 20,000
_IS_R1_OTHER_INCOME = [
    _is_row("8000", "Interest Income", "OthIncome", "2-Other Income", Decimal("12000")),
    _is_row("8010", "FX Gain", "OthIncome", "2-Other Income", Decimal("5000")),
    _is_row("8020", "Misc Income", "OthIncome", "2-Other Income", Decimal("3000")),
]
EXPECTED_OTHER_INCOME = Decimal("20000")

# 3-COGS (6 accounts): 8,700,000 + 600,000 + 400,000 + 200,000 + 70,000 + 30,000 = 10,000,000
_IS_R1_COGS = [
    _is_row("5000", "Cost of Goods Sold", "COGS", "3-COGS", Decimal("8700000")),
    _is_row("5010", "Freight In", "COGS", "3-COGS", Decimal("600000")),
    _is_row("5020", "Merchant Processing Fees", "COGS", "3-COGS", Decimal("400000")),
    _is_row("5030", "Duty and Tariffs", "COGS", "3-COGS", Decimal("200000")),
    _is_row("5040", "Packaging Costs", "COGS", "3-COGS", Decimal("70000")),
    _is_row("5050", "Inventory Shrinkage", "COGS", "3-COGS", Decimal("30000")),
]
EXPECTED_COGS = Decimal("10000000")

# 4-Operating Expense (12 accounts): sums to 1,700,000 (see running total in comments)
_IS_R1_OPEX = [
    _is_row("6000", "Salaries and Wages", "Expense", "4-Operating Expense", Decimal("650000")),
    _is_row("6010", "Marketing Expense", "Expense", "4-Operating Expense", Decimal("380000")),
    _is_row("6020", "Rent Expense", "Expense", "4-Operating Expense", Decimal("150000")),
    _is_row("6030", "Software Subscriptions", "Expense", "4-Operating Expense", Decimal("120000")),
    _is_row("6040", "Professional Fees", "Expense", "4-Operating Expense", Decimal("90000")),
    _is_row("6050", "Travel and Entertainment", "Expense", "4-Operating Expense", Decimal("60000")),
    _is_row("6060", "Insurance", "Expense", "4-Operating Expense", Decimal("50000")),
    _is_row("6070", "Utilities", "Expense", "4-Operating Expense", Decimal("40000")),
    _is_row("6080", "Office Supplies", "Expense", "4-Operating Expense", Decimal("35000")),
    _is_row("6090", "Depreciation Expense", "Expense", "4-Operating Expense", Decimal("60000")),
    _is_row("6100", "Bad Debt Expense", "Expense", "4-Operating Expense", Decimal("35000")),
    _is_row("6110", "Employee Benefits", "Expense", "4-Operating Expense", Decimal("30000")),
]
EXPECTED_OPEX = Decimal("1700000")

# 5-Other Expense (3 accounts): 9,000 + 4,000 + 2,000 = 15,000
_IS_R1_OTHER_EXPENSE = [
    _is_row("9000", "Interest Expense", "OthExpense", "5-Other Expense", Decimal("9000")),
    _is_row("9010", "Bank Fees", "OthExpense", "5-Other Expense", Decimal("4000")),
    _is_row("9020", "Loss on FX", "OthExpense", "5-Other Expense", Decimal("2000")),
]
EXPECTED_OTHER_EXPENSE = Decimal("15000")

_IS_R1_ROWS = _IS_R1_REVENUE + _IS_R1_OTHER_INCOME + _IS_R1_COGS + _IS_R1_OPEX + _IS_R1_OTHER_EXPENSE
assert len(_IS_R1_ROWS) == 30

# Derivations (hand-computed): GP = Rev - COGS; OpInc = GP - OpEx; NI = OpInc + OtherInc - OtherExp
EXPECTED_GROSS_PROFIT = EXPECTED_REVENUE - EXPECTED_COGS  # 13,500,000 - 10,000,000 = 3,500,000
EXPECTED_OPERATING_INCOME = EXPECTED_GROSS_PROFIT - EXPECTED_OPEX  # 3,500,000 - 1,700,000 = 1,800,000
EXPECTED_NET_INCOME = (
    EXPECTED_OPERATING_INCOME + EXPECTED_OTHER_INCOME - EXPECTED_OTHER_EXPENSE
)  # 1,800,000 + 20,000 - 15,000 = 1,805,000
assert EXPECTED_GROSS_PROFIT == Decimal("3500000")
assert EXPECTED_OPERATING_INCOME == Decimal("1800000")
assert EXPECTED_NET_INCOME == Decimal("1805000")

# Margins (1dp, ROUND_HALF_UP): GP 3,500,000/13,500,000=25.9259...% -> 25.9%
#   OpInc 1,800,000/13,500,000=13.3333...% -> 13.3%; NI 1,805,000/13,500,000=13.3703...% -> 13.4%
EXPECTED_GP_MARGIN_STR = "25.9%"
EXPECTED_OPINC_MARGIN_STR = "13.3%"
EXPECTED_NI_MARGIN_STR = "13.4%"

# --- r2: May 2026 (prior period) --------------------------------------------------------
_IS_R2_REVENUE = [
    _is_row("4000", "Product Sales", "Income", "1-Revenue", Decimal("12300000")),  # delta +300,000
    _is_row("4010", "Subscription Revenue", "Income", "1-Revenue", Decimal("680000")),
    _is_row("4020", "Shipping Revenue", "Income", "1-Revenue", Decimal("240000")),
    _is_row("4030", "Gift Card Redemptions", "Income", "1-Revenue", Decimal("48000")),
    _is_row("4040", "Other Sales", "Income", "1-Revenue", Decimal("145000")),
    _is_row("4900", "Sales Returns and Allowances", "Income", "1-Revenue", Decimal("-230000")),
]
# prior revenue = 12,300,000+680,000+240,000+48,000+145,000-230,000 = 13,183,000
EXPECTED_PRIOR_REVENUE = Decimal("13183000")

_IS_R2_OTHER_INCOME = [
    _is_row("8000", "Interest Income", "OthIncome", "2-Other Income", Decimal("11000")),
    _is_row("8010", "FX Gain", "OthIncome", "2-Other Income", Decimal("4500")),
    _is_row("8020", "Misc Income", "OthIncome", "2-Other Income", Decimal("3200")),
]
EXPECTED_PRIOR_OTHER_INCOME = Decimal("18700")  # 11,000+4,500+3,200

_IS_R2_COGS = [
    _is_row("5000", "Cost of Goods Sold", "COGS", "3-COGS", Decimal("8600000")),  # delta +100,000
    _is_row("5010", "Freight In", "COGS", "3-COGS", Decimal("580000")),
    _is_row("5020", "Merchant Processing Fees", "COGS", "3-COGS", Decimal("390000")),
    _is_row("5030", "Duty and Tariffs", "COGS", "3-COGS", Decimal("195000")),
    _is_row("5040", "Packaging Costs", "COGS", "3-COGS", Decimal("68000")),
    _is_row("5050", "Inventory Shrinkage", "COGS", "3-COGS", Decimal("28000")),
]
# prior COGS = 8,600,000+580,000+390,000+195,000+68,000+28,000 = 9,861,000
EXPECTED_PRIOR_COGS = Decimal("9861000")

_IS_R2_OPEX = [
    _is_row("6000", "Salaries and Wages", "Expense", "4-Operating Expense", Decimal("630000")),
    _is_row("6010", "Marketing Expense", "Expense", "4-Operating Expense", Decimal("200000")),  # delta +180,000
    _is_row("6020", "Rent Expense", "Expense", "4-Operating Expense", Decimal("150000")),
    _is_row("6030", "Software Subscriptions", "Expense", "4-Operating Expense", Decimal("110000")),
    _is_row("6040", "Professional Fees", "Expense", "4-Operating Expense", Decimal("85000")),
    _is_row("6050", "Travel and Entertainment", "Expense", "4-Operating Expense", Decimal("55000")),
    _is_row("6060", "Insurance", "Expense", "4-Operating Expense", Decimal("48000")),
    _is_row("6070", "Utilities", "Expense", "4-Operating Expense", Decimal("38000")),
    _is_row("6080", "Office Supplies", "Expense", "4-Operating Expense", Decimal("33000")),
    _is_row("6090", "Depreciation Expense", "Expense", "4-Operating Expense", Decimal("60000")),
    _is_row("6100", "Bad Debt Expense", "Expense", "4-Operating Expense", Decimal("33000")),
    _is_row("6110", "Employee Benefits", "Expense", "4-Operating Expense", Decimal("28000")),
]
# prior OpEx = 630,000+200,000+150,000+110,000+85,000+55,000+48,000+38,000+33,000+60,000+33,000+28,000
#   = 1,470,000
EXPECTED_PRIOR_OPEX = Decimal("1470000")

_IS_R2_OTHER_EXPENSE = [
    _is_row("9000", "Interest Expense", "OthExpense", "5-Other Expense", Decimal("8500")),
    _is_row("9010", "Bank Fees", "OthExpense", "5-Other Expense", Decimal("3800")),
    _is_row("9020", "Loss on FX", "OthExpense", "5-Other Expense", Decimal("1700")),
]
EXPECTED_PRIOR_OTHER_EXPENSE = Decimal("14000")  # 8,500+3,800+1,700

_IS_R2_ROWS = _IS_R2_REVENUE + _IS_R2_OTHER_INCOME + _IS_R2_COGS + _IS_R2_OPEX + _IS_R2_OTHER_EXPENSE

EXPECTED_PRIOR_GROSS_PROFIT = EXPECTED_PRIOR_REVENUE - EXPECTED_PRIOR_COGS  # 13,183,000-9,861,000=3,322,000
EXPECTED_PRIOR_OPERATING_INCOME = EXPECTED_PRIOR_GROSS_PROFIT - EXPECTED_PRIOR_OPEX  # 3,322,000-1,470,000=1,852,000
EXPECTED_PRIOR_NET_INCOME = (
    EXPECTED_PRIOR_OPERATING_INCOME + EXPECTED_PRIOR_OTHER_INCOME - EXPECTED_PRIOR_OTHER_EXPENSE
)  # 1,852,000+18,700-14,000 = 1,856,700
assert EXPECTED_PRIOR_GROSS_PROFIT == Decimal("3322000")
assert EXPECTED_PRIOR_OPERATING_INCOME == Decimal("1852000")
assert EXPECTED_PRIOR_NET_INCOME == Decimal("1856700")

# MoM deltas (money, whole dollar; pct 1dp vs abs(prior))
EXPECTED_REVENUE_MOM_DELTA_STR = "+$317,000"  # 13,500,000-13,183,000
EXPECTED_REVENUE_MOM_PCT_STR = "+2.4%"  # 317,000/13,183,000*100 = 2.40461...%
EXPECTED_GP_MOM_DELTA_STR = "+$178,000"  # 3,500,000-3,322,000
EXPECTED_GP_MOM_PCT_STR = "+5.4%"  # 178,000/3,322,000*100 = 5.35821...%
EXPECTED_OPINC_MOM_DELTA_STR = "−$52,000"  # 1,800,000-1,852,000 = -52,000
EXPECTED_OPINC_MOM_PCT_STR = "−2.8%"  # -52,000/1,852,000*100 = -2.80778...%
EXPECTED_NI_MOM_DELTA_STR = "−$51,700"  # 1,805,000-1,856,700 = -51,700
EXPECTED_NI_MOM_PCT_STR = "−2.8%"  # -51,700/1,856,700*100 = -2.78451...%

# GP margin MoM in percentage points, computed from RAW (unrounded) margins, not the
# already-rounded display strings: current 3,500,000/13,500,000=25.925925...%,
# prior 3,322,000/13,183,000=25.19911...%; delta = +0.72681...pp -> "+0.7pp"
EXPECTED_GP_MARGIN_MOM_PP_STR = "+0.7pp"
EXPECTED_PRIOR_GP_MARGIN_STR = "25.2%"

# --- r3: Jun 2025 (YoY) — section-aggregate rows only (KPI totals only need scalar sums,
# never per-account alignment — see statement_builder module docstring) ------------------
_IS_R3_ROWS = [
    _is_row("4999", "Revenue (YoY aggregate)", "Income", "1-Revenue", Decimal("11700000")),
    _is_row("8999", "Other Income (YoY aggregate)", "OthIncome", "2-Other Income", Decimal("14000")),
    _is_row("5999", "COGS (YoY aggregate)", "COGS", "3-COGS", Decimal("8700000")),
    _is_row("6999", "Operating Expense (YoY aggregate)", "Expense", "4-Operating Expense", Decimal("1500000")),
    _is_row("9999", "Other Expense (YoY aggregate)", "OthExpense", "5-Other Expense", Decimal("11000")),
]
EXPECTED_YOY_REVENUE = Decimal("11700000")
EXPECTED_YOY_GROSS_PROFIT = Decimal("11700000") - Decimal("8700000")  # 3,000,000
EXPECTED_YOY_OPERATING_INCOME = EXPECTED_YOY_GROSS_PROFIT - Decimal("1500000")  # 1,500,000
EXPECTED_YOY_NET_INCOME = EXPECTED_YOY_OPERATING_INCOME + Decimal("14000") - Decimal("11000")  # 1,503,000
assert EXPECTED_YOY_NET_INCOME == Decimal("1503000")

# YoY pct (vs abs(yoy)): Revenue (13.5M-11.7M)/11.7M=15.3846...%; GP (3.5M-3M)/3M=16.6667%;
# OpInc (1.8M-1.5M)/1.5M=20.0%; NI (1,805,000-1,503,000)/1,503,000=20.09315...%
EXPECTED_REVENUE_YOY_PCT_STR = "+15.4%"
EXPECTED_GP_YOY_PCT_STR = "+16.7%"
EXPECTED_OPINC_YOY_PCT_STR = "+20.0%"
EXPECTED_NI_YOY_PCT_STR = "+20.1%"

# --- r4: income_statement_trend, trailing 6 months Jan..Jun 2026 — one aggregate row per
# section per period (30 rows). Per-period section totals were chosen so periodname="Jun 2026"
# EXACTLY matches r1's totals and periodname="May 2026" EXACTLY matches r2's totals (same GL,
# same period, internally consistent fixture). Jan-Apr are free-choice, chosen so Jun 2026 is
# NEITHER the trailing-6 max NOR min of NI margin (watch rule 3 does not fire in this fixture
# — a dedicated smaller fixture below exercises rule 3 firing both ways).
_IS_TREND_COLUMNS = ["periodname", "startdate", "acctnumber", "acctname", "accttype", "section", "amount"]

# (periodname, startdate, revenue, other_income, cogs, opex, other_expense)
_IS_TREND_PERIODS = [
    ("Jan 2026", "2026-01-01", Decimal("11800000"), Decimal("15000"), Decimal("8850000"), Decimal("1550000"), Decimal("12000")),
    ("Feb 2026", "2026-02-01", Decimal("12100000"), Decimal("16000"), Decimal("9000000"), Decimal("1580000"), Decimal("12500")),
    ("Mar 2026", "2026-03-01", Decimal("12400000"), Decimal("17000"), Decimal("9150000"), Decimal("1610000"), Decimal("13000")),
    ("Apr 2026", "2026-04-01", Decimal("12750000"), Decimal("18000"), Decimal("9400000"), Decimal("1640000"), Decimal("13500")),
    ("May 2026", "2026-05-01", EXPECTED_PRIOR_REVENUE, EXPECTED_PRIOR_OTHER_INCOME, EXPECTED_PRIOR_COGS, EXPECTED_PRIOR_OPEX, EXPECTED_PRIOR_OTHER_EXPENSE),
    ("Jun 2026", "2026-06-01", EXPECTED_REVENUE, EXPECTED_OTHER_INCOME, EXPECTED_COGS, EXPECTED_OPEX, EXPECTED_OTHER_EXPENSE),
]  # fmt: skip

_IS_R4_ROWS: list[dict] = []
for _pname, _pdate, _rev, _oi, _cogs, _opex, _oe in _IS_TREND_PERIODS:
    _IS_R4_ROWS.extend(
        [
            {
                "periodname": _pname,
                "startdate": _pdate,
                **_is_row("4999", "Revenue (trend)", "Income", "1-Revenue", _rev),
            },
            {
                "periodname": _pname,
                "startdate": _pdate,
                **_is_row("8999", "Other Income (trend)", "OthIncome", "2-Other Income", _oi),
            },
            {"periodname": _pname, "startdate": _pdate, **_is_row("5999", "COGS (trend)", "COGS", "3-COGS", _cogs)},
            {
                "periodname": _pname,
                "startdate": _pdate,
                **_is_row("6999", "Operating Expense (trend)", "Expense", "4-Operating Expense", _opex),
            },
            {
                "periodname": _pname,
                "startdate": _pdate,
                **_is_row("9999", "Other Expense (trend)", "OthExpense", "5-Other Expense", _oe),
            },
        ]
    )
assert len(_IS_R4_ROWS) == 30

# Expected trend/spark series (chronological, Jan..Jun), hand-derived from the table above.
EXPECTED_TREND_PERIODS = ["Jan 2026", "Feb 2026", "Mar 2026", "Apr 2026", "May 2026", "Jun 2026"]
EXPECTED_TREND_REVENUE = [
    Decimal("11800000"), Decimal("12100000"), Decimal("12400000"), Decimal("12750000"),
    EXPECTED_PRIOR_REVENUE, EXPECTED_REVENUE,
]  # fmt: skip
EXPECTED_TREND_GROSS_PROFIT = [
    Decimal("11800000") - Decimal("8850000"),  # 2,950,000
    Decimal("12100000") - Decimal("9000000"),  # 3,100,000
    Decimal("12400000") - Decimal("9150000"),  # 3,250,000
    Decimal("12750000") - Decimal("9400000"),  # 3,350,000
    EXPECTED_PRIOR_GROSS_PROFIT,  # 3,322,000
    EXPECTED_GROSS_PROFIT,  # 3,500,000
]
EXPECTED_TREND_OPERATING_INCOME = [
    Decimal("2950000") - Decimal("1550000"),  # 1,400,000
    Decimal("3100000") - Decimal("1580000"),  # 1,520,000
    Decimal("3250000") - Decimal("1610000"),  # 1,640,000
    Decimal("3350000") - Decimal("1640000"),  # 1,710,000
    EXPECTED_PRIOR_OPERATING_INCOME,  # 1,852,000
    EXPECTED_OPERATING_INCOME,  # 1,800,000
]
EXPECTED_TREND_NET_INCOME = [
    Decimal("1400000") + Decimal("15000") - Decimal("12000"),  # 1,403,000
    Decimal("1520000") + Decimal("16000") - Decimal("12500"),  # 1,523,500
    Decimal("1640000") + Decimal("17000") - Decimal("13000"),  # 1,644,000
    Decimal("1710000") + Decimal("18000") - Decimal("13500"),  # 1,714,500
    EXPECTED_PRIOR_NET_INCOME,  # 1,856,700
    EXPECTED_NET_INCOME,  # 1,805,000
]
assert EXPECTED_TREND_NET_INCOME[-2] == Decimal("1856700")
assert EXPECTED_TREND_NET_INCOME[-1] == Decimal("1805000")
# NI margins per period (raw, for rule-3 max/min check): Jan 11.89%, Feb 12.59%, Mar 13.26%,
# Apr 13.45%, May 14.08% (max), Jun 13.37% (neither max nor min) -> rule 3 silent here.


def income_statement_payloads() -> dict[str, dict]:
    """Full happy-path IS fixture: r1 (current), r2 (prior), r3 (YoY), r4 (6-month trend)."""
    return {
        "r1": _payload(_IS_COLUMNS, _IS_R1_ROWS, query="income_statement (Jun 2026)"),
        "r2": _payload(_IS_COLUMNS, _IS_R2_ROWS, query="income_statement (May 2026)"),
        "r3": _payload(_IS_COLUMNS, _IS_R3_ROWS, query="income_statement (Jun 2025)"),
        "r4": _payload(_IS_TREND_COLUMNS, _IS_R4_ROWS, query="income_statement_trend (...)"),
    }


def income_statement_payloads_missing_compare() -> dict[str, dict]:
    """r2/r3/r4 entirely ABSENT from the payloads dict (Task 4's resolver never even
    attempted them, or they weren't referenced) — every compare field must degrade to
    None/omitted, never raise."""
    return {"r1": _payload(_IS_COLUMNS, _IS_R1_ROWS, query="income_statement (Jun 2026)")}


def income_statement_payloads_failed_compare() -> dict[str, dict]:
    """r2 present but explicitly failed (``success: False``); r3/r4 absent entirely — mixes
    both degradation shapes the brief calls out."""
    return {
        "r1": _payload(_IS_COLUMNS, _IS_R1_ROWS, query="income_statement (Jun 2026)"),
        "r2": _failed("source r2 (netsuite_financial_report) failed: SuiteQL execution failed: timeout"),
    }


def malformed_r1_payload() -> dict[str, dict]:
    """r1 has neither columns+rows nor items — the ONE case that must raise ValueError
    (compose fail-closed depends on this)."""
    return {"r1": {"success": True, "kind": "table", "unexpected": "shape"}}


def income_statement_payloads_zero_rows() -> dict[str, dict]:
    """r1 with a well-formed but EMPTY row set (real columns, zero rows) -- the SQL
    ran fine but returned no accounts at all. A statement with literally zero GL
    accounts is a data/connection problem, not a legitimate "quiet period" render (a
    genuinely quiet account still HAS a $0 line; zero accounts means nothing posted
    to ANY tracked account type, which never happens for a real tenant) -- must raise,
    never silently produce an all-zero statement."""
    return {"r1": _payload(_IS_COLUMNS, [], query="income_statement (Jun 2026)")}


def income_statement_string_amount_payloads() -> dict[str, dict]:
    """A minimal (2-account) IS payload whose ``amount`` cells are STRINGS, mirroring how
    SuiteQL actually serializes numeric cells (see ``fmt_amount`` docstring in report_html.py).
    Proves the Decimal(str(v)) parse boundary handles string input, not just Python Decimal/
    int/float. Revenue "1000000.50" + COGS "-400000.25" style rows; no compare sources."""
    rows = [
        {
            "acctnumber": "4000",
            "acctname": "Product Sales",
            "accttype": "Income",
            "section": "1-Revenue",
            "amount": "1000000.50",
        },
        {
            "acctnumber": "5000",
            "acctname": "Cost of Goods Sold",
            "accttype": "COGS",
            "section": "3-COGS",
            "amount": "400000.25",
        },
    ]
    return {"r1": _payload(_IS_COLUMNS, rows, query="income_statement (Jun 2026)")}


EXPECTED_STRING_AMOUNT_REVENUE = Decimal("1000000.50")
EXPECTED_STRING_AMOUNT_COGS = Decimal("400000.25")
EXPECTED_STRING_AMOUNT_GP = Decimal("600000.25")


def income_statement_items_derived_payload() -> dict[str, dict]:
    """r1 in the DEFENSIVE ``items`` (list-of-dicts) shape instead of columns+rows — proves
    the parser is defensive about the items-derived form per the brief."""
    items = [
        {
            "acctnumber": "4000",
            "acctname": "Product Sales",
            "accttype": "Income",
            "section": "1-Revenue",
            "amount": Decimal("500000"),
        },
        {
            "acctnumber": "5000",
            "acctname": "Cost of Goods Sold",
            "accttype": "COGS",
            "section": "3-COGS",
            "amount": Decimal("200000"),
        },
    ]
    return {"r1": {"kind": "table", "items": items}}


EXPECTED_ITEMS_SHAPE_REVENUE = Decimal("500000")
EXPECTED_ITEMS_SHAPE_GP = Decimal("300000")


def misaligned_account_payloads() -> dict[str, dict]:
    """Minimal r1/r2 pair exercising acctnumber alignment: account 4001 is NEW this period
    (exists only in r1 -> prior defaults to 0, "still list the account"); account 4002 had
    activity ONLY last period and none this period (exists only in r2 -> current defaults to
    0, still listed). Account 4000 is common to both (normal delta)."""
    r1_rows = [
        _is_row("4000", "Product Sales", "Income", "1-Revenue", Decimal("500000")),
        _is_row("4001", "New Product Line", "Income", "1-Revenue", Decimal("40000")),
    ]
    r2_rows = [
        _is_row("4000", "Product Sales", "Income", "1-Revenue", Decimal("470000")),
        _is_row("4002", "Discontinued Line", "Income", "1-Revenue", Decimal("15000")),
    ]
    return {
        "r1": _payload(_IS_COLUMNS, r1_rows, query="income_statement (Jun 2026)"),
        "r2": _payload(_IS_COLUMNS, r2_rows, query="income_statement (May 2026)"),
    }


EXPECTED_MISALIGNED_4000_DELTA = Decimal("30000")  # 500,000 - 470,000
EXPECTED_MISALIGNED_4001_CURRENT = Decimal("40000")
EXPECTED_MISALIGNED_4001_PRIOR = Decimal("0")  # absent from r2 -> treated as 0
EXPECTED_MISALIGNED_4001_DELTA = Decimal("40000")
EXPECTED_MISALIGNED_4002_CURRENT = Decimal("0")  # absent from r1 -> treated as 0, still listed
EXPECTED_MISALIGNED_4002_PRIOR = Decimal("15000")
EXPECTED_MISALIGNED_4002_DELTA = Decimal("-15000")


def ni_driver_cross_section_payloads() -> dict[str, dict]:
    """Minimal fixture proving the NI-driver highlight (H1) scans ALL five IS sections for
    the largest |delta| mover -- not just the "obvious" revenue/COGS/opex ones. The largest
    mover here sits in Other Expense (Interest Expense, +80,000), deliberately larger than
    the Revenue (+50,000) and Operating Expense (+40,000) movers, both of which a
    revenue/COGS/opex-only scan would wrongly prefer over the true largest mover."""
    r1_rows = [
        _is_row("4000", "Base Sales", "Income", "1-Revenue", Decimal("10000000")),
        _is_row("4040", "Other Sales", "Income", "1-Revenue", Decimal("260000")),  # delta +50,000
        _is_row("5000", "Cost of Goods Sold", "COGS", "3-COGS", Decimal("4000000")),  # delta 0
        _is_row("6030", "Software Subscriptions", "Expense", "4-Operating Expense", Decimal("300000")),  # delta +40,000
        _is_row(
            "9000", "Interest Expense", "OthExpense", "5-Other Expense", Decimal("100000")
        ),  # delta +80,000 (largest)
    ]
    r2_rows = [
        _is_row("4000", "Base Sales", "Income", "1-Revenue", Decimal("10000000")),
        _is_row("4040", "Other Sales", "Income", "1-Revenue", Decimal("210000")),
        _is_row("5000", "Cost of Goods Sold", "COGS", "3-COGS", Decimal("4000000")),
        _is_row("6030", "Software Subscriptions", "Expense", "4-Operating Expense", Decimal("260000")),
        _is_row("9000", "Interest Expense", "OthExpense", "5-Other Expense", Decimal("20000")),
    ]
    return {
        "r1": _payload(_IS_COLUMNS, r1_rows, query="income_statement (Jun 2026)"),
        "r2": _payload(_IS_COLUMNS, r2_rows, query="income_statement (May 2026)"),
    }


# current: revenue=10,260,000, GP=6,260,000, OpInc=5,960,000, NI=5,960,000-100,000=5,860,000
# prior:   revenue=10,210,000, GP=6,210,000, OpInc=5,950,000, NI=5,950,000-20,000=5,930,000
EXPECTED_NI_DRIVER_NI_DELTA_STR = "−$70,000"  # 5,860,000-5,930,000 = -70,000
EXPECTED_NI_DRIVER_NAME = "Interest Expense"
EXPECTED_NI_DRIVER_DELTA_STR = "+$80,000"  # the driver's OWN natural delta (not sign-flipped)


def ni_margin_trend_best_payloads() -> dict[str, dict]:
    """Minimal IS + trend fixture where the CURRENT period's NI margin is the trailing-6
    MAX -> watch rule 3 fires with tone "good", text mentions "best month in trailing 6"."""
    columns = _IS_COLUMNS
    trend_columns = _IS_TREND_COLUMNS
    r1_rows = [
        _is_row("4000", "Sales", "Income", "1-Revenue", Decimal("1000000")),
        _is_row("9999", "Net Income Passthrough", "OthIncome", "2-Other Income", Decimal("200000")),
    ]
    # current NI margin = 200,000/1,000,000*100 = 20.0% -- the best of the trailing 6 below
    periods = [
        ("Jan 2026", "2026-01-01", Decimal("1000000"), Decimal("100000")),
        ("Feb 2026", "2026-02-01", Decimal("1000000"), Decimal("110000")),
        ("Mar 2026", "2026-03-01", Decimal("1000000"), Decimal("120000")),
        ("Apr 2026", "2026-04-01", Decimal("1000000"), Decimal("130000")),
        ("May 2026", "2026-05-01", Decimal("1000000"), Decimal("140000")),
        ("Jun 2026", "2026-06-01", Decimal("1000000"), Decimal("200000")),
    ]
    trend_rows = []
    for pname, pdate, rev, ni in periods:
        trend_rows.append(
            {"periodname": pname, "startdate": pdate, **_is_row("4000", "Sales", "Income", "1-Revenue", rev)}
        )
        trend_rows.append(
            {
                "periodname": pname,
                "startdate": pdate,
                **_is_row("9999", "Net Income Passthrough", "OthIncome", "2-Other Income", ni),
            }
        )
    return {
        "r1": _payload(columns, r1_rows, query="income_statement (Jun 2026)"),
        "r4": _payload(trend_columns, trend_rows, query="income_statement_trend (...)"),
    }


def ni_margin_trend_worst_payloads() -> dict[str, dict]:
    """Mirror of ``ni_margin_trend_best_payloads`` where the CURRENT period is the trailing-6
    MIN -> watch rule 3 fires with tone "bad", text mentions "worst month in trailing 6"."""
    columns = _IS_COLUMNS
    trend_columns = _IS_TREND_COLUMNS
    r1_rows = [
        _is_row("4000", "Sales", "Income", "1-Revenue", Decimal("1000000")),
        _is_row("9999", "Net Income Passthrough", "OthIncome", "2-Other Income", Decimal("50000")),
    ]
    # current NI margin = 50,000/1,000,000*100 = 5.0% -- the worst of the trailing 6 below
    periods = [
        ("Jan 2026", "2026-01-01", Decimal("1000000"), Decimal("200000")),
        ("Feb 2026", "2026-02-01", Decimal("1000000"), Decimal("190000")),
        ("Mar 2026", "2026-03-01", Decimal("1000000"), Decimal("180000")),
        ("Apr 2026", "2026-04-01", Decimal("1000000"), Decimal("170000")),
        ("May 2026", "2026-05-01", Decimal("1000000"), Decimal("160000")),
        ("Jun 2026", "2026-06-01", Decimal("1000000"), Decimal("50000")),
    ]
    trend_rows = []
    for pname, pdate, rev, ni in periods:
        trend_rows.append(
            {"periodname": pname, "startdate": pdate, **_is_row("4000", "Sales", "Income", "1-Revenue", rev)}
        )
        trend_rows.append(
            {
                "periodname": pname,
                "startdate": pdate,
                **_is_row("9999", "Net Income Passthrough", "OthIncome", "2-Other Income", ni),
            }
        )
    return {
        "r1": _payload(columns, r1_rows, query="income_statement (Jun 2026)"),
        "r4": _payload(trend_columns, trend_rows, query="income_statement_trend (...)"),
    }


def ni_margin_trend_cross_year_live_date_format_payloads() -> dict[str, dict]:
    """Cross-year trailing-6 window (Aug 2026 -> Jan 2027) with LIVE-SuiteQL-format
    startdates ("M/D/YYYY", NOT the ISO "YYYY-MM-DD" every other trend fixture in this
    module uses) -- proves ``_trend_periods`` orders buckets by the AUTHORITATIVE
    ``periodname``, never a lexicographic string sort over ``startdate``.

    A raw string sort scrambles this window: "10/1/2026" < "8/1/2026" (comparing the first
    character, '1' < '8'), and "1/1/2027" sorts before ALL of them ('/' < '0') -- so the
    buggy order was [Jan 2027, Oct 2026, Nov 2026, Dec 2026, Aug 2026, Sep 2026], putting
    the TRUE CURRENT period (Jan 2027) first and misattributing its data to whichever bucket
    the wrong sort put last (Sep 2026).

    NI margins climb monotonically Aug->Dec (10%..14%) then jump to 20% for the true
    current month (Jan 2027) -- the trailing-6 MAX. Watch rule 3 (``margins[-1]``) only
    fires "best month" when the last bucket is genuinely Jan 2027; under the buggy order,
    ``margins[-1]`` resolves to Sep 2026's 11% -- neither the max (20%) nor the min (10%)
    of the set -- so rule 3 stays silent under the bug, a clean structural tell independent
    of visually inspecting bucket order.

    Uses a plain Revenue - COGS structure (COGS = 1,000,000 - desired NI) so NI equals the
    desired figure EXACTLY, with OpEx/OtherIncome/OtherExpense all zero -- NOT a bare
    "other income" account added on TOP of revenue (that would make NI = revenue +
    other-income, not other-income alone)."""
    r1_rows = [
        _is_row("4000", "Sales", "Income", "1-Revenue", Decimal("1000000")),
        _is_row("5000", "COGS", "COGS", "3-COGS", Decimal("800000")),  # NI = 1,000,000-800,000 = 200,000
    ]
    # (periodname, LIVE-format "M/D/YYYY" startdate, revenue, cogs) -- chronological Aug->Jan
    periods = [
        ("Aug 2026", "8/1/2026", Decimal("1000000"), Decimal("900000")),  # NI 100,000 -> 10.0%
        ("Sep 2026", "9/1/2026", Decimal("1000000"), Decimal("890000")),  # NI 110,000 -> 11.0%
        ("Oct 2026", "10/1/2026", Decimal("1000000"), Decimal("880000")),  # NI 120,000 -> 12.0%
        ("Nov 2026", "11/1/2026", Decimal("1000000"), Decimal("870000")),  # NI 130,000 -> 13.0%
        ("Dec 2026", "12/1/2026", Decimal("1000000"), Decimal("860000")),  # NI 140,000 -> 14.0%
        ("Jan 2027", "1/1/2027", Decimal("1000000"), Decimal("800000")),  # NI 200,000 -> 20.0% (true current, max)
    ]
    trend_rows = []
    for pname, pdate, rev, cogs in periods:
        trend_rows.append(
            {"periodname": pname, "startdate": pdate, **_is_row("4000", "Sales", "Income", "1-Revenue", rev)}
        )
        trend_rows.append({"periodname": pname, "startdate": pdate, **_is_row("5000", "COGS", "COGS", "3-COGS", cogs)})
    return {
        "r1": _payload(_IS_COLUMNS, r1_rows, query="income_statement (Jan 2027)"),
        "r4": _payload(_IS_TREND_COLUMNS, trend_rows, query="income_statement_trend (...)"),
    }


EXPECTED_CROSS_YEAR_TREND_PERIODS = ["Aug 2026", "Sep 2026", "Oct 2026", "Nov 2026", "Dec 2026", "Jan 2027"]


def malformed_r1_amount_payload() -> dict[str, dict]:
    """r1 with a non-numeric amount value -- ``Decimal(str(...))`` raises
    ``decimal.InvalidOperation`` for this, which ``_to_decimal`` must translate to
    ``ValueError`` (the exception type ``assemble_spec``'s fail-closed ``except ValueError``
    seam actually catches)."""
    rows = [_is_row("4000", "Sales", "Income", "1-Revenue", "not-a-number")]
    return {"r1": _payload(_IS_COLUMNS, rows, query="income_statement (Jun 2026)")}


def nonfinite_r1_amount_payload() -> dict[str, dict]:
    """r1 with a non-finite amount ("nan") -- ``Decimal("nan")`` constructs successfully
    (it's a valid Decimal special value) but is not finite; ``_to_decimal`` must reject it
    as ``ValueError`` up front rather than letting a later ``.quantize()`` explode with
    ``decimal.InvalidOperation`` instead."""
    rows = [_is_row("4000", "Sales", "Income", "1-Revenue", "nan")]
    return {"r1": _payload(_IS_COLUMNS, rows, query="income_statement (Jun 2026)")}


def malformed_prior_amount_payloads() -> dict[str, dict]:
    """r1 clean; r2 (prior) has a junk amount on its one row -- must degrade the WHOLE
    prior comparison to None (never raise), the same as an absent or ``success: False``
    r2, since a compare-source data problem must never crash the primary statement."""
    r1_rows = [_is_row("4000", "Sales", "Income", "1-Revenue", Decimal("1000000"))]
    r2_rows = [_is_row("4000", "Sales", "Income", "1-Revenue", "garbage")]
    return {
        "r1": _payload(_IS_COLUMNS, r1_rows, query="income_statement (Jun 2026)"),
        "r2": _payload(_IS_COLUMNS, r2_rows, query="income_statement (May 2026)"),
    }


def row_cap_boundary_payloads(row_count: int) -> dict[str, dict]:
    """A synthetic r1 with exactly ``row_count`` distinct 1-Revenue accounts (each $1) --
    tests the STATEMENT_ROW_CAP guard at its exact boundary without hand-writing thousands
    of literal rows. Total revenue == row_count dollars, trivial to hand-verify."""
    rows = [_is_row(str(4000 + i), f"Account {i}", "Income", "1-Revenue", Decimal("1")) for i in range(row_count)]
    return {"r1": _payload(_IS_COLUMNS, rows, query="income_statement (Jun 2026)")}


def bs_row_cap_boundary_payloads(row_count: int) -> dict[str, dict]:
    """Balance-sheet analogue of ``row_cap_boundary_payloads`` -- exercises the same
    STATEMENT_ROW_CAP guard wired into the balance_sheet builder (which otherwise has no
    watch mechanism at all)."""
    rows = [_bs_row(str(1000 + i), f"Asset {i}", "Bank", "1-Assets", Decimal("1")) for i in range(row_count)]
    return {"r1": _payload(_BS_COLUMNS, rows, query="balance_sheet (2026-06-30)")}


def tb_row_cap_boundary_payloads(row_count: int) -> dict[str, dict]:
    """Trial-balance analogue of ``row_cap_boundary_payloads``."""
    rows = [_tb_row(str(1000 + i), f"Account {i}", "Bank", "1", "0") for i in range(row_count)]
    return {"r1": _payload(_TB_COLUMNS, rows, query="trial_balance (Jun 2026)")}


def truncated_r1_payload(present_rows: int = 100, true_total: int = 6000) -> dict[str, dict]:
    """T2 gate B1(b): r1 with a well-shaped account list that is ITSELF truncated -- exactly
    the shape a ``_cap_stored_rows``-capped extraction produces (``truncated=True``,
    ``row_count`` the TRUE pre-cap total, ``len(rows)`` the smaller surviving count).
    ``_require_rows`` must fail closed on this rather than silently building a statement
    from a partial account list -- distinct from ``row_cap_boundary_payloads`` (which is
    NOT truncated; every row it declares is genuinely present)."""
    rows = [_is_row(str(4000 + i), f"Account {i}", "Income", "1-Revenue", Decimal("1")) for i in range(present_rows)]
    payload = _payload(_IS_COLUMNS, rows, query="income_statement (Jun 2026)")
    payload["truncated"] = True
    payload["row_count"] = true_total
    return {"r1": payload}


def truncated_compare_payload(present_rows: int = 100, true_total: int = 6000) -> dict[str, dict]:
    """T2 gate B1(b): r1 is a normal, complete payload; r2 (prior) is truncated the same
    way ``truncated_r1_payload`` is. ``_resolve_rows`` must degrade (None) rather than
    compute deltas/margins against a partial prior-period account list."""
    r1_rows = [_is_row(str(4000 + i), f"Account {i}", "Income", "1-Revenue", Decimal("1")) for i in range(5)]
    r2_rows = [_is_row(str(4000 + i), f"Account {i}", "Income", "1-Revenue", Decimal("1")) for i in range(present_rows)]
    r2_payload = _payload(_IS_COLUMNS, r2_rows, query="income_statement (May 2026)")
    r2_payload["truncated"] = True
    r2_payload["row_count"] = true_total
    return {
        "r1": _payload(_IS_COLUMNS, r1_rows, query="income_statement (Jun 2026)"),
        "r2": r2_payload,
    }


def empty_compare_payload() -> dict[str, dict]:
    """T2 gate M-B: r2 (prior) is present, well-shaped, and legitimately EMPTY (a derived
    compare period with zero rows -- fiscal-calendar drift, an empty period). Must degrade
    (None) exactly like an absent/failed r2, never compute deltas/margins against a $0
    prior. r1 is a normal 5-account payload (its OWN zero-row case is covered separately
    by ``income_statement_payloads_zero_rows``, which must still raise)."""
    r1_rows = [_is_row(str(4000 + i), f"Account {i}", "Income", "1-Revenue", Decimal("1")) for i in range(5)]
    return {
        "r1": _payload(_IS_COLUMNS, r1_rows, query="income_statement (Jun 2026)"),
        "r2": _payload(_IS_COLUMNS, [], query="income_statement (May 2026)"),
    }


def negative_revenue_mover_payloads() -> dict[str, dict]:
    """T2 gate F-1: a net-negative-revenue period (refund-heavy month -- revenue held
    CONSTANT at -$100,000 both periods, so the revenue line itself is never a "mover")
    with one IMMATERIAL OpEx mover ($300 delta, well under both the 1%-of-|revenue|
    mover threshold and the 0.5%-of-|revenue| highlight threshold) and one MATERIAL OpEx
    mover ($2000 delta, well over both) -- proves the materiality gates key off
    ``abs(revenue)``, not revenue's raw sign. Before the fix, a negative revenue makes
    ``threshold_dollars`` negative, so ``abs(delta) >= threshold_dollars`` is vacuously
    true for EVERY mover regardless of magnitude, including the immaterial one."""
    r1_rows = [
        _is_row("4000", "Refund-Heavy Revenue", "Income", "1-Revenue", Decimal("-100000")),
        _is_row("6000", "Immaterial Mover", "Expense", "4-Operating Expense", Decimal("5300")),
        _is_row("6010", "Material Mover", "Expense", "4-Operating Expense", Decimal("7000")),
    ]
    r2_rows = [
        _is_row("4000", "Refund-Heavy Revenue", "Income", "1-Revenue", Decimal("-100000")),
        _is_row("6000", "Immaterial Mover", "Expense", "4-Operating Expense", Decimal("5000")),
        _is_row("6010", "Material Mover", "Expense", "4-Operating Expense", Decimal("5000")),
    ]
    return {
        "r1": _payload(_IS_COLUMNS, r1_rows, query="income_statement (Jun 2026)"),
        "r2": _payload(_IS_COLUMNS, r2_rows, query="income_statement (May 2026)"),
    }


def negative_revenue_only_immaterial_mover_payloads() -> dict[str, dict]:
    """T2 gate F-1 regression (round-3 reviewer's recommended discriminating test): a
    net-negative-revenue period with EXACTLY ONE mover, and it's IMMATERIAL for the
    highlights gate ($100 delta on |revenue|=$50,000 -- 0.2%, well under the 0.5%
    threshold). No highlight of any kind should fire. Unlike
    ``negative_revenue_mover_payloads`` (which always has a genuinely material mover
    present, so a broken threshold can hide behind it still firing correctly), THIS
    fixture has no material mover at all -- if the highlights threshold's ``abs()``
    regresses back to bare ``revenue``, this negative-revenue threshold flips negative
    and the immaterial mover incorrectly clears it, firing a highlight where none
    should exist."""
    r1_rows = [
        _is_row("4000", "Refund-Heavy Revenue", "Income", "1-Revenue", Decimal("-50000")),
        _is_row("6000", "Immaterial Mover", "Expense", "4-Operating Expense", Decimal("5100")),
    ]
    r2_rows = [
        _is_row("4000", "Refund-Heavy Revenue", "Income", "1-Revenue", Decimal("-50000")),
        _is_row("6000", "Immaterial Mover", "Expense", "4-Operating Expense", Decimal("5000")),
    ]
    return {
        "r1": _payload(_IS_COLUMNS, r1_rows, query="income_statement (Jun 2026)"),
        "r2": _payload(_IS_COLUMNS, r2_rows, query="income_statement (May 2026)"),
    }


def trend_row_cap_boundary_payloads(row_count: int) -> dict[str, dict]:
    """T2 gate F-2: r1 is a normal 5-account payload; r4 (trend) has exactly
    ``row_count`` synthetic account x period rows, all in a single synthetic period --
    tests the trend-at-cap degrade guard at its exact boundary. The SQL cap is baked
    INSIDE the trend query's FETCH FIRST clause, so a capped trend source's own
    ``totalResults`` reflects the CAPPED count (never flags ``truncated``) -- this
    fixture mimics exactly that: a well-shaped, non-truncated payload that nonetheless
    lands at/above STATEMENT_ROW_CAP."""
    r1_rows = [_is_row(str(4000 + i), f"Account {i}", "Income", "1-Revenue", Decimal("1")) for i in range(5)]
    trend_rows = [
        {
            "periodname": "Jun 2026",
            "startdate": "2026-06-01",
            "acctnumber": str(4000 + i),
            "acctname": f"Account {i}",
            "accttype": "Income",
            "section": "1-Revenue",
            "amount": Decimal("1"),
        }
        for i in range(row_count)
    ]
    return {
        "r1": _payload(_IS_COLUMNS, r1_rows, query="income_statement (Jun 2026)"),
        "r4": _payload(_IS_TREND_COLUMNS, trend_rows, query="income_statement_trend (...)"),
    }


def prior_at_cap_payloads(row_count: int) -> dict[str, dict]:
    """T2 gate F-2: r1 normal (5 accounts); r2 (prior) has exactly ``row_count``
    synthetic accounts -- tests _resolve_rows's generic at-cap degrade check for
    ordinary (non-trend) compare rids (prior/yoy)."""
    r1_rows = [_is_row(str(4000 + i), f"Account {i}", "Income", "1-Revenue", Decimal("1")) for i in range(5)]
    r2_rows = [_is_row(str(4000 + i), f"Account {i}", "Income", "1-Revenue", Decimal("1")) for i in range(row_count)]
    return {
        "r1": _payload(_IS_COLUMNS, r1_rows, query="income_statement (Jun 2026)"),
        "r2": _payload(_IS_COLUMNS, r2_rows, query="income_statement (May 2026)"),
    }


def sign_flipping_contra_revenue_payloads() -> dict[str, dict]:
    """T2 gate F-3: a Revenue-section account that's POSITIVE in the current period
    ($5,000 -- ordinary revenue, does not reduce profit) but was NEGATIVE (contra) in the
    prior period (-$3,000 -- reduces profit). The prior CELL's parens convention must key
    off the PRIOR amount's own sign, not the current amount's."""
    r1_rows = [_is_row("4099", "Contra Revenue Adj", "Income", "1-Revenue", Decimal("5000"))]
    r2_rows = [_is_row("4099", "Contra Revenue Adj", "Income", "1-Revenue", Decimal("-3000"))]
    return {
        "r1": _payload(_IS_COLUMNS, r1_rows, query="income_statement (Jun 2026)"),
        "r2": _payload(_IS_COLUMNS, r2_rows, query="income_statement (May 2026)"),
    }


def gp_margin_watch_payloads(delta_pp_direction: str) -> dict[str, dict]:
    """Minimal 2-account (1 revenue + 1 COGS) r1/r2 pair whose GP margin MoM delta lands
    EXACTLY at the watch-rule-1 threshold boundary (0.3pp), for straddling tests.

    ``delta_pp_direction``:
      - "just_over": raw |delta| = 0.30000...1pp (just past the threshold) -> watch fires.
      - "just_under": raw |delta| = 0.29999...9pp (just short) -> watch does NOT fire.

    Revenue is fixed at 1,000,000 both periods (so margin == GP/1,000,000, i.e. GP in
    dollars maps 1:1 to margin in percentage points -- makes the boundary trivial to hit
    exactly: GP of X dollars on 1,000,000 revenue is an X/10,000 pp margin).
    """
    revenue = Decimal("1000000")
    prior_gp_margin = Decimal("20.0")  # prior COGS = 800,000 -> GP 200,000 -> margin 20.0%
    if delta_pp_direction == "just_over":
        current_margin = prior_gp_margin + Decimal("0.3000001")
    elif delta_pp_direction == "just_under":
        current_margin = prior_gp_margin + Decimal("0.2999999")
    else:
        raise ValueError(f"unknown delta_pp_direction: {delta_pp_direction!r}")
    current_gp = revenue * current_margin / Decimal("100")
    current_cogs = revenue - current_gp
    prior_gp = revenue * prior_gp_margin / Decimal("100")
    prior_cogs = revenue - prior_gp
    r1_rows = [
        _is_row("4000", "Sales", "Income", "1-Revenue", revenue),
        _is_row("5000", "COGS", "COGS", "3-COGS", current_cogs),
    ]
    r2_rows = [
        _is_row("4000", "Sales", "Income", "1-Revenue", revenue),
        _is_row("5000", "COGS", "COGS", "3-COGS", prior_cogs),
    ]
    return {
        "r1": _payload(_IS_COLUMNS, r1_rows, query="income_statement (Jun 2026)"),
        "r2": _payload(_IS_COLUMNS, r2_rows, query="income_statement (May 2026)"),
    }


def account_mover_watch_payloads(delta_pct_direction: str) -> dict[str, dict]:
    """Minimal r1/r2 pair whose single OpEx-account MoM delta lands EXACTLY at the
    watch-rule-2 threshold boundary (1% of revenue = 10,000 on a FIXED 1,000,000 revenue).

    The mover lives in OpEx (not revenue) specifically so its own delta can never shift the
    revenue denominator it's being measured against -- a same-account mover-and-denominator
    fixture would make the 1%-of-revenue boundary a moving target instead of an exact line."""
    revenue = Decimal("1000000")
    opex_prior = Decimal("300000")
    if delta_pct_direction == "just_over":
        delta = Decimal("10000.01")
    elif delta_pct_direction == "just_under":
        delta = Decimal("9999.99")
    else:
        raise ValueError(f"unknown delta_pct_direction: {delta_pct_direction!r}")
    opex_current = opex_prior + delta
    r1_rows = [
        _is_row("4000", "Sales", "Income", "1-Revenue", revenue),
        _is_row("6010", "Marketing Expense", "Expense", "4-Operating Expense", opex_current),
    ]
    r2_rows = [
        _is_row("4000", "Sales", "Income", "1-Revenue", revenue),
        _is_row("6010", "Marketing Expense", "Expense", "4-Operating Expense", opex_prior),
    ]
    return {
        "r1": _payload(_IS_COLUMNS, r1_rows, query="income_statement (Jun 2026)"),
        "r2": _payload(_IS_COLUMNS, r2_rows, query="income_statement (May 2026)"),
    }


def duplicate_account_name_watch_payloads() -> dict[str, dict]:
    """Two accounts sharing the SAME acctname ("Consulting") in DIFFERENT sections --
    NetSuite does not enforce acctname uniqueness. Proves watch rule 2's tone is resolved
    from the mover's OWN section (threaded through the movers tuple), not a fragile
    name-based re-lookup that would silently give both accounts the same tone.

    Base Sales (revenue, unchanged) keeps total revenue fixed at 10,500,000, so the 1%
    threshold (105,000) is unaffected by either mover. Revenue "Consulting" grows
    200,000 -> 500,000 (delta +300,000, favorable -> "good"); OpEx "Consulting" grows
    200,000 -> 400,000 (delta +200,000, unfavorable -> "warn"). Different magnitudes give
    an unambiguous ranking (larger mover first) so the two watch items are distinguishable
    by position without relying on tone (the very thing under test)."""
    r1_rows = [
        _is_row("4000", "Base Sales", "Income", "1-Revenue", Decimal("10000000")),
        _is_row("4010", "Consulting", "Income", "1-Revenue", Decimal("500000")),
        _is_row("6010", "Consulting", "Expense", "4-Operating Expense", Decimal("400000")),
    ]
    r2_rows = [
        _is_row("4000", "Base Sales", "Income", "1-Revenue", Decimal("10000000")),
        _is_row("4010", "Consulting", "Income", "1-Revenue", Decimal("200000")),
        _is_row("6010", "Consulting", "Expense", "4-Operating Expense", Decimal("200000")),
    ]
    return {
        "r1": _payload(_IS_COLUMNS, r1_rows, query="income_statement (Jun 2026)"),
        "r2": _payload(_IS_COLUMNS, r2_rows, query="income_statement (May 2026)"),
    }


EXPECTED_DUP_NAME_REVENUE_MOVER_TEXT = "Consulting +$300,000 MoM (+150.0%)"
EXPECTED_DUP_NAME_OPEX_MOVER_TEXT = "Consulting +$200,000 MoM (+100.0%)"


def highlight_threshold_payloads(delta_direction: str) -> dict[str, dict]:
    """Minimal r1/r2 pair whose single COGS-account MoM delta lands EXACTLY at the
    highlight threshold boundary (0.5% of revenue = 5,000 on 1,000,000 revenue), isolating
    the "GP margin driven by largest COGS mover" highlight (H2)."""
    revenue = Decimal("1000000")
    cogs_prior = Decimal("400000")
    if delta_direction == "just_over":
        delta = Decimal("5000.01")
    elif delta_direction == "just_under":
        delta = Decimal("4999.99")
    else:
        raise ValueError(f"unknown delta_direction: {delta_direction!r}")
    cogs_current = cogs_prior + delta
    r1_rows = [
        _is_row("4000", "Sales", "Income", "1-Revenue", revenue),
        _is_row("5000", "COGS", "COGS", "3-COGS", cogs_current),
    ]
    r2_rows = [
        _is_row("4000", "Sales", "Income", "1-Revenue", revenue),
        _is_row("5000", "COGS", "COGS", "3-COGS", cogs_prior),
    ]
    return {
        "r1": _payload(_IS_COLUMNS, r1_rows, query="income_statement (Jun 2026)"),
        "r2": _payload(_IS_COLUMNS, r2_rows, query="income_statement (May 2026)"),
    }


# ===========================================================================
# BALANCE SHEET — balanced (Assets = Liabilities + Equity), both periods
# ===========================================================================

_BS_COLUMNS = ["acctnumber", "acctname", "accttype", "section", "balance"]


def _bs_row(number: str, name: str, accttype: str, section: str, balance) -> dict:
    return {"acctnumber": number, "acctname": name, "accttype": accttype, "section": section, "balance": balance}


_BS_R1_ASSETS = [
    _bs_row("1000", "Cash", "Bank", "1-Assets", Decimal("2500000")),
    _bs_row("1010", "Accounts Receivable", "AcctRec", "1-Assets", Decimal("1200000")),
    _bs_row("1020", "Inventory", "OthCurrAsset", "1-Assets", Decimal("1800000")),
    _bs_row("1030", "Prepaid Expenses", "OthCurrAsset", "1-Assets", Decimal("150000")),
    _bs_row("1500", "Fixed Assets, net", "FixedAsset", "1-Assets", Decimal("900000")),
]
EXPECTED_BS_ASSETS = Decimal("6550000")  # 2,500,000+1,200,000+1,800,000+150,000+900,000

_BS_R1_LIABILITIES = [
    _bs_row("2000", "Accounts Payable", "AcctPay", "2-Liabilities", Decimal("850000")),
    _bs_row("2010", "Accrued Liabilities", "OthCurrLiab", "2-Liabilities", Decimal("300000")),
    _bs_row("2020", "Deferred Revenue", "DeferRevenue", "2-Liabilities", Decimal("400000")),
    _bs_row("2500", "Long-term Debt", "LongTermLiab", "2-Liabilities", Decimal("1200000")),
]
EXPECTED_BS_LIABILITIES = Decimal("2750000")  # 850,000+300,000+400,000+1,200,000

_BS_R1_EQUITY = [
    _bs_row("3000", "Common Stock", "Equity", "3-Equity", Decimal("500000")),
    _bs_row("3010", "Retained Earnings", "Equity", "3-Equity", Decimal("3300000")),
]
EXPECTED_BS_EQUITY = Decimal("3800000")  # 500,000+3,300,000

_BS_R1_ROWS = _BS_R1_ASSETS + _BS_R1_LIABILITIES + _BS_R1_EQUITY
assert EXPECTED_BS_ASSETS == EXPECTED_BS_LIABILITIES + EXPECTED_BS_EQUITY  # 6,550,000 == 6,550,000

_BS_R2_ASSETS = [
    _bs_row("1000", "Cash", "Bank", "1-Assets", Decimal("2350000")),
    _bs_row("1010", "Accounts Receivable", "AcctRec", "1-Assets", Decimal("1150000")),
    _bs_row("1020", "Inventory", "OthCurrAsset", "1-Assets", Decimal("1750000")),
    _bs_row("1030", "Prepaid Expenses", "OthCurrAsset", "1-Assets", Decimal("140000")),
    _bs_row("1500", "Fixed Assets, net", "FixedAsset", "1-Assets", Decimal("910000")),
]
EXPECTED_BS_PRIOR_ASSETS = Decimal("6300000")  # 2,350,000+1,150,000+1,750,000+140,000+910,000

_BS_R2_LIABILITIES = [
    _bs_row("2000", "Accounts Payable", "AcctPay", "2-Liabilities", Decimal("800000")),
    _bs_row("2010", "Accrued Liabilities", "OthCurrLiab", "2-Liabilities", Decimal("280000")),
    _bs_row("2020", "Deferred Revenue", "DeferRevenue", "2-Liabilities", Decimal("380000")),
    _bs_row("2500", "Long-term Debt", "LongTermLiab", "2-Liabilities", Decimal("1220000")),
]
EXPECTED_BS_PRIOR_LIABILITIES = Decimal("2680000")  # 800,000+280,000+380,000+1,220,000

_BS_R2_EQUITY = [
    _bs_row("3000", "Common Stock", "Equity", "3-Equity", Decimal("500000")),
    _bs_row("3010", "Retained Earnings", "Equity", "3-Equity", Decimal("3120000")),
]
EXPECTED_BS_PRIOR_EQUITY = Decimal("3620000")  # 500,000+3,120,000

_BS_R2_ROWS = _BS_R2_ASSETS + _BS_R2_LIABILITIES + _BS_R2_EQUITY
assert EXPECTED_BS_PRIOR_ASSETS == EXPECTED_BS_PRIOR_LIABILITIES + EXPECTED_BS_PRIOR_EQUITY  # 6,300,000

EXPECTED_BS_ASSETS_MOM_DELTA_STR = "+$250,000"  # 6,550,000-6,300,000
EXPECTED_BS_LIABILITIES_MOM_DELTA_STR = "+$70,000"  # 2,750,000-2,680,000
EXPECTED_BS_EQUITY_MOM_DELTA_STR = "+$180,000"  # 3,800,000-3,620,000


def balance_sheet_payloads() -> dict[str, dict]:
    return {
        "r1": _payload(_BS_COLUMNS, _BS_R1_ROWS, query="balance_sheet (2026-06-30)"),
        "r2": _payload(_BS_COLUMNS, _BS_R2_ROWS, query="balance_sheet (2026-05-31)"),
    }


def balance_sheet_payloads_missing_compare() -> dict[str, dict]:
    return {"r1": _payload(_BS_COLUMNS, _BS_R1_ROWS, query="balance_sheet (2026-06-30)")}


def balance_sheet_payloads_zero_rows() -> dict[str, dict]:
    """Balance-sheet analogue of ``income_statement_payloads_zero_rows``."""
    return {"r1": _payload(_BS_COLUMNS, [], query="balance_sheet (2026-06-30)")}


def balance_sheet_unbalanced_payloads() -> dict[str, dict]:
    """Assets deliberately off by 1,000 from Liabilities + Equity -- the check must fail."""
    rows = (
        _BS_R1_ASSETS
        + _BS_R1_LIABILITIES
        + _BS_R1_EQUITY[:-1]
        + [
            _bs_row("3010", "Retained Earnings", "Equity", "3-Equity", Decimal("3299000")),  # was 3,300,000
        ]
    )
    return {"r1": _payload(_BS_COLUMNS, rows, query="balance_sheet (2026-06-30)")}


EXPECTED_BS_UNBALANCED_DIFF = Decimal("1000")


# ===========================================================================
# TRIAL BALANCE — balanced (debits = credits), both periods. NO section column.
# ===========================================================================

_TB_COLUMNS = ["acctnumber", "acctname", "accttype", "total_debit", "total_credit", "net_amount"]


def _tb_row(number: str, name: str, accttype: str, debit, credit) -> dict:
    debit = Decimal(debit)
    credit = Decimal(credit)
    return {
        "acctnumber": number,
        "acctname": name,
        "accttype": accttype,
        "total_debit": debit,
        "total_credit": credit,
        "net_amount": debit - credit,
    }


_TB_R1_ROWS = [
    _tb_row("1000", "Cash", "Bank", "1000000", "0"),
    _tb_row("2000", "Accounts Payable", "AcctPay", "0", "300000"),
    _tb_row("3000", "Common Stock", "Equity", "0", "200000"),
    _tb_row("3010", "Retained Earnings", "Equity", "0", "200000"),
    _tb_row("4000", "Sales Revenue", "Income", "0", "900000"),
    _tb_row("5000", "Cost of Goods Sold", "COGS", "600000", "0"),
]
EXPECTED_TB_TOTAL_DEBIT = Decimal("1600000")  # 1,000,000+600,000
EXPECTED_TB_TOTAL_CREDIT = Decimal("1600000")  # 300,000+200,000+200,000+900,000
assert sum((r["total_debit"] for r in _TB_R1_ROWS), Decimal("0")) == EXPECTED_TB_TOTAL_DEBIT
assert sum((r["total_credit"] for r in _TB_R1_ROWS), Decimal("0")) == EXPECTED_TB_TOTAL_CREDIT

_TB_R2_ROWS = [
    _tb_row("1000", "Cash", "Bank", "930000", "0"),
    _tb_row("2000", "Accounts Payable", "AcctPay", "0", "280000"),
    _tb_row("3000", "Common Stock", "Equity", "0", "200000"),
    _tb_row("3010", "Retained Earnings", "Equity", "0", "150000"),
    _tb_row("4000", "Sales Revenue", "Income", "0", "850000"),
    _tb_row("5000", "Cost of Goods Sold", "COGS", "550000", "0"),
]
EXPECTED_TB_PRIOR_TOTAL_DEBIT = Decimal("1480000")  # 930,000+550,000
EXPECTED_TB_PRIOR_TOTAL_CREDIT = Decimal("1480000")  # 280,000+200,000+150,000+850,000

# MoM deltas: both sides move identically since both periods are balanced
# (1,600,000-1,480,000=120,000; 120,000/1,480,000*100=8.1081...% -> 8.1%)
EXPECTED_TB_DEBIT_MOM_DELTA_STR = "+$120,000"
EXPECTED_TB_DEBIT_MOM_PCT_STR = "+8.1%"
EXPECTED_TB_CREDIT_MOM_DELTA_STR = "+$120,000"
EXPECTED_TB_CREDIT_MOM_PCT_STR = "+8.1%"
assert sum((r["total_debit"] for r in _TB_R2_ROWS), Decimal("0")) == EXPECTED_TB_PRIOR_TOTAL_DEBIT
assert sum((r["total_credit"] for r in _TB_R2_ROWS), Decimal("0")) == EXPECTED_TB_PRIOR_TOTAL_CREDIT

EXPECTED_TB_CASH_DELTA = Decimal("70000")  # 1,000,000-930,000 (net_amount delta)


def trial_balance_payloads() -> dict[str, dict]:
    return {
        "r1": _payload(_TB_COLUMNS, _TB_R1_ROWS, query="trial_balance (Jun 2026)"),
        "r2": _payload(_TB_COLUMNS, _TB_R2_ROWS, query="trial_balance (May 2026)"),
    }


def trial_balance_payloads_missing_compare() -> dict[str, dict]:
    return {"r1": _payload(_TB_COLUMNS, _TB_R1_ROWS, query="trial_balance (Jun 2026)")}


def trial_balance_payloads_zero_rows() -> dict[str, dict]:
    """Trial-balance analogue of ``income_statement_payloads_zero_rows``."""
    return {"r1": _payload(_TB_COLUMNS, [], query="trial_balance (Jun 2026)")}


def trial_balance_unbalanced_payloads() -> dict[str, dict]:
    """Debits deliberately exceed credits by 5,000 -- the in-balance check must fail."""
    rows = list(_TB_R1_ROWS)
    rows[-1] = _tb_row("5000", "Cost of Goods Sold", "COGS", "605000", "0")  # was 600,000
    return {"r1": _payload(_TB_COLUMNS, rows, query="trial_balance (Jun 2026)")}


EXPECTED_TB_UNBALANCED_DIFF = Decimal("5000")
