"""Deterministic financial report tool — verified SQL templates, no LLM generation.

Each template is a known-good SuiteQL query verified against NetSuite native reports.
The only variable is the period filter, which is substituted safely.
"""

from __future__ import annotations

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
