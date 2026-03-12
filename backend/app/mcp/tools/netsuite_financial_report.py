"""Deterministic financial report tool — verified SQL templates, no LLM generation.

Each template is a known-good SuiteQL query verified against NetSuite native reports.
The only variable is the period filter, which is substituted safely.
"""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta

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
    params: dict | None = None,
    context: dict | None = None,
    *,
    report_type: str | None = None,
    period: str | None = None,
    tenant_id: str | None = None,
    db=None,
    subsidiary_id: int | None = None,
) -> dict:
    """Run a verified financial report template.

    Supports two calling conventions:
    1. MCP registry: execute(params_dict, context=context_dict)
    2. Direct: execute(report_type=..., period=..., tenant_id=..., db=...)
    """
    # Unpack MCP registry calling convention
    if params is not None:
        report_type = report_type or params.get("report_type", "")
        period = period or params.get("period", "")
        subsidiary_id = subsidiary_id or params.get("subsidiary_id")
    if context is not None:
        tenant_id = tenant_id or context.get("tenant_id")
        db = db or context.get("db")

    if not report_type or not period:
        return {"success": False, "error": "report_type and period are required."}
    if not tenant_id or not db:
        return {"success": False, "error": "tenant_id and db are required (via context)."}

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
        "error": result.get("message") or result.get("error"),
    }


# ---------------------------------------------------------------------------
# Intent parser — detect report type + period from natural language
# ---------------------------------------------------------------------------

_MONTH_NAMES = list(_MONTH_END.keys())
_MONTH_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+(\d{4})\b",
    re.IGNORECASE,
)
_RELATIVE_PERIOD_RE = re.compile(
    r"\b(last|previous|prior|this|current)\s+(month|quarter|year)\b", re.IGNORECASE
)
_Q_RE = re.compile(r"\bQ([1-4])\s+(\d{4})\b", re.IGNORECASE)


def _normalize_month(month_str: str) -> str:
    """Convert 'January' → 'Jan', 'february' → 'Feb', etc."""
    return month_str[:3].capitalize()


def _resolve_relative_period(ref: str, unit: str) -> str:
    """Resolve 'last month' → 'Feb 2026', 'this quarter' → 'Jan 2026, Feb 2026, Mar 2026'."""
    now = datetime.now(tz=None)

    if unit.lower() == "month":
        if ref.lower() in ("last", "previous", "prior"):
            first_of_month = now.replace(day=1)
            last_month = first_of_month - timedelta(days=1)
            return f"{_MONTH_NAMES[last_month.month - 1]} {last_month.year}"
        else:  # this/current
            return f"{_MONTH_NAMES[now.month - 1]} {now.year}"

    elif unit.lower() == "quarter":
        if ref.lower() in ("last", "previous", "prior"):
            q = (now.month - 1) // 3  # current quarter 0-indexed
            if q == 0:
                # Last quarter of previous year
                months = [10, 11, 12]
                year = now.year - 1
            else:
                first_month = (q - 1) * 3 + 1
                months = [first_month, first_month + 1, first_month + 2]
                year = now.year
        else:  # this/current
            q = (now.month - 1) // 3
            first_month = q * 3 + 1
            months = [first_month, first_month + 1, first_month + 2]
            year = now.year
        return ", ".join(f"{_MONTH_NAMES[m - 1]} {year}" for m in months)

    elif unit.lower() == "year":
        if ref.lower() in ("last", "previous", "prior"):
            year = now.year - 1
        else:
            year = now.year
        return ", ".join(f"{_MONTH_NAMES[m]} {year}" for m in range(12))

    return f"{_MONTH_NAMES[now.month - 1]} {now.year}"


def _resolve_quarter(q_num: str, year: str) -> str:
    """Resolve 'Q1 2026' → 'Jan 2026, Feb 2026, Mar 2026'."""
    q = int(q_num)
    first_month = (q - 1) * 3 + 1
    months = [first_month, first_month + 1, first_month + 2]
    return ", ".join(f"{_MONTH_NAMES[m - 1]} {year}" for m in months)


def parse_report_intent(user_message: str) -> dict | None:
    """Parse a user message to extract report_type and period.

    Returns {"report_type": ..., "period": ...} or None if not parseable.
    """
    msg = user_message.lower()

    # Determine report type
    report_type = None
    if any(kw in msg for kw in ("balance sheet", "bs ")):
        if any(kw in msg for kw in ("trend", "month over month", "by month", "by period", "compare")):
            report_type = "balance_sheet_trend"
        else:
            report_type = "balance_sheet"
    elif any(kw in msg for kw in ("trial balance", "tb ")):
        report_type = "trial_balance"
    elif any(kw in msg for kw in (
        "income statement", "p&l", "profit and loss", "profit & loss",
        "p/l", "pl ", "revenue", "expense", "cogs", "net income",
    )):
        if any(kw in msg for kw in ("trend", "month over month", "by month", "by period", "compare")):
            report_type = "income_statement_trend"
        else:
            report_type = "income_statement"
    else:
        # Generic financial — default to income statement
        report_type = "income_statement"

    # Determine period
    period = None

    # Try explicit months: "Feb 2026", "January 2025"
    month_matches = _MONTH_RE.findall(user_message)
    if month_matches:
        periods = [f"{_normalize_month(m)} {y}" for m, y in month_matches]
        period = ", ".join(periods)
        # Multiple months → auto-upgrade to trend if not already
        if len(periods) > 1 and "_trend" not in report_type:
            report_type += "_trend"

    # Try quarter: "Q1 2026"
    if not period:
        q_match = _Q_RE.search(user_message)
        if q_match:
            period = _resolve_quarter(q_match.group(1), q_match.group(2))
            if "_trend" not in report_type:
                report_type += "_trend"

    # Try relative: "last month", "this quarter"
    if not period:
        rel_match = _RELATIVE_PERIOD_RE.search(user_message)
        if rel_match:
            period = _resolve_relative_period(rel_match.group(1), rel_match.group(2))
            # If quarter/year → trend
            if rel_match.group(2).lower() in ("quarter", "year") and "_trend" not in report_type:
                report_type += "_trend"

    if not period:
        return None  # Can't determine period — let the agent handle it

    return {"report_type": report_type, "period": period}
