"""Deterministic financial report tool — verified SQL templates, no LLM generation.

Each template is a known-good SuiteQL query verified against NetSuite native reports.
The only variable is the period filter, which is substituted safely.
"""

from __future__ import annotations

import re

# Strict validation: only allow "Mon YYYY" format or "YYYY-MM-DD" date format
_PERIOD_NAME_RE = re.compile(r"^[A-Z][a-z]{2}\s\d{4}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_MONTH_END: dict[str, str] = {
    "Jan": "31", "Feb": "28", "Mar": "31", "Apr": "30",
    "May": "31", "Jun": "30", "Jul": "31", "Aug": "31",
    "Sep": "30", "Oct": "31", "Nov": "30", "Dec": "31",
}


def _validate_period_name(period: str) -> None:
    """Validate a single period name against injection attacks."""
    period = period.strip()
    if not _PERIOD_NAME_RE.match(period) and not _DATE_RE.match(period):
        raise ValueError(
            f"Invalid period format: '{period}'. "
            "Expected 'Mon YYYY' (e.g., 'Feb 2026') or 'YYYY-MM-DD' (e.g., '2026-02-28')."
        )


def _period_to_end_date(period: str) -> str:
    """Convert 'Mon YYYY' to the last day of that month as 'YYYY-MM-DD'."""
    parts = period.strip().split()
    month_abbr, year = parts[0], parts[1]
    month_num = list(_MONTH_END.keys()).index(month_abbr) + 1

    day = _MONTH_END[month_abbr]
    if month_abbr == "Feb":
        y = int(year)
        if (y % 4 == 0 and y % 100 != 0) or (y % 400 == 0):
            day = "29"

    return f"{year}-{month_num:02d}-{day}"


def build_period_filter(period_mode: str, period: str) -> str:
    """Build a safe SQL WHERE fragment for the given period.

    Args:
        period_mode: "single_period", "multi_period", or "inception_to_date"
        period: Period name(s) like "Feb 2026" or "Jan 2026, Feb 2026"
                or a date like "2026-01-31"

    Returns:
        SQL fragment like "ap.periodname = 'Feb 2026'" or
        "ap.enddate <= TO_DATE('2026-02-28', 'YYYY-MM-DD')"
    """
    if not period or not period.strip():
        raise ValueError("Period is required — provide e.g., 'Feb 2026' or '2026-02-28'.")

    period = period.strip()

    if period_mode == "inception_to_date":
        if _DATE_RE.match(period):
            return f"ap.enddate <= TO_DATE('{period}', 'YYYY-MM-DD')"
        _validate_period_name(period)
        end_date = _period_to_end_date(period)
        return f"ap.enddate <= TO_DATE('{end_date}', 'YYYY-MM-DD')"

    # single_period and multi_period both use ap.periodname
    periods = [p.strip() for p in period.split(",")]
    for p in periods:
        _validate_period_name(p)

    if len(periods) == 1:
        return f"ap.periodname = '{periods[0]}'"
    else:
        quoted = ", ".join(f"'{p}'" for p in periods)
        return f"ap.periodname IN ({quoted})"


REPORT_TEMPLATES: dict[str, dict] = {
    "income_statement": {
        "description": "Income Statement (P&L) for a specific period or date range",
        "period_mode": "single_period",
        "sql_template": """SELECT
    a.acctnumber,
    a.acctname,
    a.accttype,
    CASE
        WHEN a.accttype = 'Income'     THEN '1-Revenue'
        WHEN a.accttype = 'OthIncome'  THEN '2-Other Income'
        WHEN a.accttype = 'COGS'       THEN '3-COGS'
        WHEN a.accttype = 'Expense'    THEN '4-Operating Expense'
        WHEN a.accttype = 'OthExpense' THEN '5-Other Expense'
    END AS section,
    SUM(tal.amount) * CASE WHEN a.accttype IN ('Income', 'OthIncome') THEN -1 ELSE 1 END AS amount
FROM transactionaccountingline tal
    JOIN transaction t ON t.id = tal.transaction
    JOIN account a ON a.id = tal.account
    JOIN accountingperiod ap ON ap.id = t.postingperiod
WHERE tal.posting = 'T'
    AND tal.accountingbook = (SELECT id FROM accountingbook WHERE isprimary = 'T')
    AND a.accttype IN ('Income', 'OthIncome', 'COGS', 'Expense', 'OthExpense')
    AND (COALESCE(a.eliminate, 'F') = 'F' OR a.acctnumber = '4990')
    AND ap.isquarter = 'F' AND ap.isyear = 'F'
    AND {period_filter}
GROUP BY a.acctnumber, a.acctname, a.accttype
HAVING SUM(tal.amount) <> 0
ORDER BY section, a.acctnumber
FETCH FIRST 500 ROWS ONLY""",
    },
    "balance_sheet": {
        "description": "Balance Sheet as of a specific date (inception-to-date)",
        "period_mode": "inception_to_date",
        "sql_template": """SELECT
    a.acctnumber,
    a.acctname,
    a.accttype,
    CASE
        WHEN a.accttype IN ('Bank','AcctRec','UnbilledRec','OthCurrAsset','FixedAsset','OthAsset','DeferExpense') THEN '1-Assets'
        WHEN a.accttype IN ('AcctPay','CreditCard','OthCurrLiab','LongTermLiab','DeferRevenue') THEN '2-Liabilities'
        WHEN a.accttype = 'Equity' THEN '3-Equity'
    END AS section,
    SUM(tal.amount * CASE
        WHEN a.accttype IN ('AcctPay','CreditCard','OthCurrLiab','LongTermLiab','DeferRevenue','Equity') THEN -1
        ELSE 1
    END) AS balance
FROM transactionaccountingline tal
    JOIN transaction t ON t.id = tal.transaction
    JOIN account a ON a.id = tal.account
    JOIN accountingperiod ap ON ap.id = t.postingperiod
WHERE tal.posting = 'T'
    AND tal.accountingbook = (SELECT id FROM accountingbook WHERE isprimary = 'T')
    AND ap.isquarter = 'F' AND ap.isyear = 'F'
    AND a.accttype IN ('Bank','AcctRec','UnbilledRec','OthCurrAsset','FixedAsset','OthAsset','DeferExpense',
                        'AcctPay','CreditCard','OthCurrLiab','LongTermLiab','DeferRevenue','Equity')
    AND COALESCE(a.eliminate, 'F') = 'F'
    AND {period_filter}
GROUP BY a.acctnumber, a.acctname, a.accttype
HAVING SUM(tal.amount) <> 0
ORDER BY section, a.acctnumber
FETCH FIRST 500 ROWS ONLY""",
    },
    "trial_balance": {
        "description": "Trial Balance for a specific period — all GL accounts with debit/credit totals",
        "period_mode": "single_period",
        "sql_template": """SELECT
    a.acctnumber,
    a.acctname,
    a.accttype,
    SUM(tal.debit) AS total_debit,
    SUM(tal.credit) AS total_credit,
    SUM(tal.amount) AS net_amount
FROM transactionaccountingline tal
    JOIN transaction t ON t.id = tal.transaction
    JOIN account a ON a.id = tal.account
    JOIN accountingperiod ap ON ap.id = t.postingperiod
WHERE tal.posting = 'T'
    AND tal.accountingbook = (SELECT id FROM accountingbook WHERE isprimary = 'T')
    AND ap.isquarter = 'F' AND ap.isyear = 'F'
    AND a.accttype != 'Statistical'
    AND {period_filter}
GROUP BY a.acctnumber, a.acctname, a.accttype
HAVING SUM(tal.debit) <> 0 OR SUM(tal.credit) <> 0
ORDER BY a.acctnumber
FETCH FIRST 500 ROWS ONLY""",
    },
    "income_statement_trend": {
        "description": "Income Statement trend by period — one row per account per month for period-over-period analysis",
        "period_mode": "multi_period",
        "sql_template": """SELECT
    ap.periodname,
    ap.startdate,
    a.acctnumber,
    a.acctname,
    a.accttype,
    CASE
        WHEN a.accttype = 'Income'     THEN '1-Revenue'
        WHEN a.accttype = 'OthIncome'  THEN '2-Other Income'
        WHEN a.accttype = 'COGS'       THEN '3-COGS'
        WHEN a.accttype = 'Expense'    THEN '4-Operating Expense'
        WHEN a.accttype = 'OthExpense' THEN '5-Other Expense'
    END AS section,
    SUM(tal.amount) * CASE WHEN a.accttype IN ('Income', 'OthIncome') THEN -1 ELSE 1 END AS amount
FROM transactionaccountingline tal
    JOIN transaction t ON t.id = tal.transaction
    JOIN account a ON a.id = tal.account
    JOIN accountingperiod ap ON ap.id = t.postingperiod
WHERE tal.posting = 'T'
    AND tal.accountingbook = (SELECT id FROM accountingbook WHERE isprimary = 'T')
    AND a.accttype IN ('Income', 'OthIncome', 'COGS', 'Expense', 'OthExpense')
    AND (COALESCE(a.eliminate, 'F') = 'F' OR a.acctnumber = '4990')
    AND ap.isquarter = 'F' AND ap.isyear = 'F'
    AND {period_filter}
GROUP BY ap.periodname, ap.startdate, a.acctnumber, a.acctname, a.accttype
HAVING SUM(tal.amount) <> 0
ORDER BY a.acctnumber, ap.startdate
FETCH FIRST 2000 ROWS ONLY""",
    },
    "balance_sheet_trend": {
        "description": "Balance Sheet trend by period — inception-to-date balances recalculated at each period end",
        "period_mode": "multi_period",
        "sql_template": """SELECT
    ap_period.periodname,
    ap_period.startdate,
    a.acctnumber,
    a.acctname,
    a.accttype,
    CASE
        WHEN a.accttype IN ('Bank','AcctRec','UnbilledRec','OthCurrAsset','FixedAsset','OthAsset','DeferExpense') THEN '1-Assets'
        WHEN a.accttype IN ('AcctPay','CreditCard','OthCurrLiab','LongTermLiab','DeferRevenue') THEN '2-Liabilities'
        WHEN a.accttype = 'Equity' THEN '3-Equity'
    END AS section,
    SUM(tal.amount * CASE
        WHEN a.accttype IN ('AcctPay','CreditCard','OthCurrLiab','LongTermLiab','DeferRevenue','Equity') THEN -1
        ELSE 1
    END) AS balance
FROM transactionaccountingline tal
    JOIN transaction t ON t.id = tal.transaction
    JOIN account a ON a.id = tal.account
    JOIN accountingperiod ap ON ap.id = t.postingperiod
    CROSS JOIN (
        SELECT id, periodname, startdate, enddate
        FROM accountingperiod
        WHERE isquarter = 'F' AND isyear = 'F'
        AND {period_filter}
    ) ap_period
WHERE tal.posting = 'T'
    AND tal.accountingbook = (SELECT id FROM accountingbook WHERE isprimary = 'T')
    AND ap.isquarter = 'F' AND ap.isyear = 'F'
    AND ap.enddate <= ap_period.enddate
    AND a.accttype IN ('Bank','AcctRec','UnbilledRec','OthCurrAsset','FixedAsset','OthAsset','DeferExpense',
                        'AcctPay','CreditCard','OthCurrLiab','LongTermLiab','DeferRevenue','Equity')
    AND COALESCE(a.eliminate, 'F') = 'F'
GROUP BY ap_period.periodname, ap_period.startdate, a.acctnumber, a.acctname, a.accttype
HAVING SUM(tal.amount) <> 0
ORDER BY a.acctnumber, ap_period.startdate
FETCH FIRST 2000 ROWS ONLY""",
    },
}


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------

from app.mcp.tools import netsuite_suiteql as _suiteql_mod


async def _execute_suiteql(*, query: str, tenant_id: str, db, limit: int = 500) -> dict:
    """Thin wrapper around suiteql.execute() — exists for easy test mocking."""
    return await _suiteql_mod.execute(
        params={"query": query, "limit": limit},
        context={"tenant_id": tenant_id, "db": db},
    )


async def execute(
    *,
    report_type: str,
    period: str,
    tenant_id: str,
    db,
    subsidiary_id: int | None = None,
) -> dict:
    """Run a verified financial report template.

    Args:
        report_type: One of the keys in REPORT_TEMPLATES
        period: Period name like "Feb 2026" or comma-separated for multi-period
        tenant_id: Tenant UUID string
        db: AsyncSession
        subsidiary_id: Optional — filter to a single subsidiary

    Returns:
        Dict with success, report_type, period, columns, items, total_rows
    """
    if report_type not in REPORT_TEMPLATES:
        valid = ", ".join(sorted(REPORT_TEMPLATES.keys()))
        return {
            "success": False,
            "error": f"Unknown report type: '{report_type}'. Valid types: {valid}",
        }

    template = REPORT_TEMPLATES[report_type]

    try:
        period_filter = build_period_filter(template["period_mode"], period)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    sql = template["sql_template"].replace("{period_filter}", period_filter)

    if subsidiary_id is not None:
        sql = sql.replace(
            "WHERE tal.posting = 'T'",
            f"WHERE tal.posting = 'T'\n    AND t.subsidiary = {int(subsidiary_id)}",
        )

    print(f"[FINANCIAL_REPORT] type={report_type} period={period}", flush=True)
    print(f"[FINANCIAL_REPORT] SQL:\n{sql}", flush=True)

    try:
        result = await _execute_suiteql(query=sql, tenant_id=tenant_id, db=db)
    except Exception as e:
        return {"success": False, "error": f"SuiteQL execution failed: {str(e)}"}

    return {
        "success": result.get("success", not result.get("error", False)),
        "report_type": report_type,
        "period": period,
        "description": template["description"],
        "columns": result.get("columns", []),
        "items": result.get("items", []),
        "total_rows": result.get("total_rows", 0),
        "error": result.get("error") or result.get("message"),
    }
