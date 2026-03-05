---
topic_tags: ["suiteql", "financial-reports", "income-statement", "balance-sheet", "gl", "accounting"]
source_type: documentation
---

# Financial Statements via SuiteQL

## TransactionAccountingLine — The GL Ledger

The `transactionaccountingline` (TAL) table is the authoritative General Ledger. Every posted transaction writes debit/credit rows here. Use TAL for all financial statement queries (P&L, Balance Sheet, Trial Balance).

Key columns:
- `transaction` — FK to `transaction.id`
- `transactionline` — FK to `transactionline.id`
- `account` — FK to `account.id` (GL account)
- `accountingbook` — FK to `accountingbook.id` (CRITICAL: filter to primary book)
- `debit` — Debit amount in base currency (NULL if credit)
- `credit` — Credit amount in base currency (NULL if debit)
- `amount` — Net amount (debit positive, credit negative)
- `posting` — `'T'` = posts to GL

**MANDATORY filters for ALL financial queries:**
```sql
WHERE t.posting = 'T'
  AND tal.accountingbook = (SELECT id FROM accountingbook WHERE isprimary = 'T')
```

Without the `accountingbook` filter, multi-book accounting duplicates every row (2x or 3x totals).

**Correct join path** (prevents duplication):
```sql
FROM transactionaccountingline tal
INNER JOIN transaction t ON t.id = tal.transaction
INNER JOIN account a ON a.id = tal.account
```

For GL aggregation you do NOT need to join `transactionline`. Only join it if you need item-level detail.

## Account Types (`account.accttype`)

The `account` table has `accttype` — the GL account classification. Always filter by these exact string values.

### Income Statement Account Types

| `accttype` | Report Section | Normal Balance |
|-----------|---------------|----------------|
| `'Income'` | Revenue | Credit (amount is negative) |
| `'OthIncome'` | Other Income | Credit (amount is negative) |
| `'COGS'` | Cost of Goods Sold | Debit (amount is positive) |
| `'Expense'` | Operating Expenses | Debit (amount is positive) |
| `'OthExpense'` | Other Expenses | Debit (amount is positive) |

### Balance Sheet Account Types

| `accttype` | BS Section | Normal Balance |
|-----------|-----------|----------------|
| `'Bank'` | Assets | Debit |
| `'AcctRec'` | Assets | Debit |
| `'UnbilledRec'` | Assets | Debit |
| `'OthCurrAsset'` | Assets | Debit |
| `'FixedAsset'` | Assets | Debit |
| `'OthAsset'` | Assets | Debit |
| `'DeferExpense'` | Assets | Debit |
| `'AcctPay'` | Liabilities | Credit |
| `'CreditCard'` | Liabilities | Credit |
| `'OthCurrLiab'` | Liabilities | Credit |
| `'LongTermLiab'` | Liabilities | Credit |
| `'DeferRevenue'` | Liabilities | Credit |
| `'Equity'` | Equity | Credit |

Always exclude `'Statistical'` accounts — they are non-monetary and inflate totals.

Key columns on `account`:
- `acctnumber` — Account number (e.g., `'4000'`)
- `accountsearchdisplaynamecopy` — Full display name with number
- `accttype` — Account type code (see tables above)
- `eliminate` — `'T'` if intercompany elimination account

## AccountingPeriod Table

The `accountingperiod` table stores fiscal periods (months, quarters, years).

Key columns:
- `periodname` — Human name (e.g., `'Jan 2025'`)
- `startdate`, `enddate` — Period boundaries
- `isposting` — `'T'` = transactions can post here
- `isquarter` — `'T'` = quarterly summary period (exclude from GL queries)
- `isyear` — `'T'` = annual summary period (exclude from GL queries)
- `isadjust` — `'T'` = adjustment period (13th month)
- `closed` — `'T'` = period is closed

**CRITICAL**: Always filter `isquarter = 'F' AND isyear = 'F'` when joining to TAL. Quarter/year rows are rollup entries whose date ranges overlap monthly periods — including them does NOT double-count TAL data (TAL stores monthly period IDs only), but their date ranges will match your WHERE clause and confuse results.

```sql
-- Find current accounting period
SELECT id, periodname, startdate, enddate
FROM accountingperiod
WHERE SYSDATE BETWEEN startdate AND enddate
  AND isposting = 'T' AND isquarter = 'F' AND isyear = 'F'

-- Find all monthly periods in fiscal year 2025
SELECT id, periodname, startdate, enddate
FROM accountingperiod
WHERE isposting = 'T' AND isquarter = 'F' AND isyear = 'F'
  AND startdate >= TO_DATE('2025-01-01', 'YYYY-MM-DD')
  AND enddate <= TO_DATE('2025-12-31', 'YYYY-MM-DD')
ORDER BY startdate
```

## Income Statement (P&L) Query

Revenue sign convention: `tal.amount` is NEGATIVE for Income/OthIncome accounts (credit normal balance). Multiply by `-1` to get positive revenue figures.

```sql
-- P&L for a date range (e.g., January 2025)
SELECT
    a.acctnumber,
    a.accountsearchdisplaynamecopy AS account_name,
    a.accttype,
    CASE
        WHEN a.accttype = 'Income'     THEN '1-Revenue'
        WHEN a.accttype = 'OthIncome'  THEN '2-Other Income'
        WHEN a.accttype = 'COGS'       THEN '3-COGS'
        WHEN a.accttype = 'Expense'    THEN '4-Operating Expense'
        WHEN a.accttype = 'OthExpense' THEN '5-Other Expense'
    END AS section,
    SUM(tal.amount * CASE WHEN a.accttype IN ('Income', 'OthIncome') THEN -1 ELSE 1 END) AS amount
FROM transactionaccountingline tal
INNER JOIN transaction t ON t.id = tal.transaction
INNER JOIN account a ON a.id = tal.account
INNER JOIN accountingperiod ap ON ap.id = t.postingperiod
WHERE t.posting = 'T'
  AND tal.accountingbook = (SELECT id FROM accountingbook WHERE isprimary = 'T')
  AND ap.isyear = 'F' AND ap.isquarter = 'F'
  AND ap.startdate >= TO_DATE('2025-01-01', 'YYYY-MM-DD')
  AND ap.enddate <= TO_DATE('2025-01-31', 'YYYY-MM-DD')
  AND a.accttype IN ('Income', 'COGS', 'Expense', 'OthIncome', 'OthExpense')
  AND COALESCE(a.eliminate, 'F') = 'F'
GROUP BY a.acctnumber, a.accountsearchdisplaynamecopy, a.accttype
HAVING SUM(tal.amount) <> 0
ORDER BY section, a.acctnumber
FETCH FIRST 200 ROWS ONLY
```

Net Income = Total Revenue + Other Income - COGS - Operating Expenses - Other Expenses.

For **YTD P&L**, widen the date range: `ap.startdate >= TO_DATE('2025-01-01', 'YYYY-MM-DD') AND ap.enddate <= TO_DATE('2025-12-31', 'YYYY-MM-DD')`.

## Balance Sheet Query

**THE FUNDAMENTAL RULE**: Balance sheet = inception-to-date. Do NOT use a start date filter. Only filter by an end date. Adding a start date destroys the balance sheet because it excludes opening balances and retained earnings.

```sql
-- Balance Sheet as of January 31, 2025 (inception-to-date — NO start date!)
SELECT
    a.acctnumber,
    a.accountsearchdisplaynamecopy AS account_name,
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
INNER JOIN transaction t ON t.id = tal.transaction
INNER JOIN account a ON a.id = tal.account
INNER JOIN accountingperiod ap ON ap.id = t.postingperiod
WHERE tal.posting = 'T'
  AND tal.accountingbook = (SELECT id FROM accountingbook WHERE isprimary = 'T')
  AND ap.enddate <= TO_DATE('2025-01-31', 'YYYY-MM-DD')
  AND ap.isquarter = 'F' AND ap.isyear = 'F'
  AND a.accttype IN ('Bank','AcctRec','UnbilledRec','OthCurrAsset','FixedAsset','OthAsset','DeferExpense',
                      'AcctPay','CreditCard','OthCurrLiab','LongTermLiab','DeferRevenue','Equity')
  AND COALESCE(a.eliminate, 'F') = 'F'
GROUP BY a.acctnumber, a.accountsearchdisplaynamecopy, a.accttype
HAVING SUM(tal.amount) <> 0
ORDER BY section, a.acctnumber
FETCH FIRST 300 ROWS ONLY
```

## Multi-Subsidiary Consolidation

For single-subsidiary reports, add `AND t.subsidiary = <id>`.

For consolidated multi-subsidiary reports with currency translation, use `BUILTIN.CONSOLIDATE`. This function translates child subsidiary amounts into the parent subsidiary's currency using period-specific exchange rates — exactly matching NetSuite native financial reports.

**Function signature:**
```
BUILTIN.CONSOLIDATE(amount_field, view_type, rate_type, subsidiary_rate, target_subsidiary_id, period_id, book)
```

| Parameter | Values | Purpose |
|-----------|--------|---------|
| `amount_field` | `tal.amount` | The monetary field to consolidate |
| `view_type` | `'INCOME'` or `'LEDGER'` | `INCOME` = P&L (uses average rate), `LEDGER` = Balance Sheet (uses current/period-end rate) |
| `rate_type` | `'DEFAULT'`, `'STANDARD'`, `'BUDGET'` | Which consolidation rate set. Use `'DEFAULT'`. |
| `subsidiary_rate` | `'DEFAULT'`, `'CURRENT'`, `'HISTORICAL'`, `'AVERAGE'` | Override rate logic. Use `'DEFAULT'` to let NetSuite pick based on account type. |
| `target_subsidiary_id` | numeric ID | Parent subsidiary to consolidate into (e.g., `1` for top-level parent) |
| `period_id` | `ap.id` | The accounting period ID for rate lookup |
| `book` | `'DEFAULT'` or book ID | Accounting book. Use `'DEFAULT'` for primary. |

**P&L consolidation example (uses average exchange rate):**
```sql
SELECT
    a.acctnumber,
    BUILTIN.DF(a.accttype) AS account_type,
    SUM(BUILTIN.CONSOLIDATE(tal.amount, 'INCOME', 'DEFAULT', 'DEFAULT', 1, ap.id, 'DEFAULT')
        * CASE WHEN a.accttype IN ('Income','OthIncome') THEN -1 ELSE 1 END
    ) AS consolidated_amount
FROM transactionaccountingline tal
INNER JOIN transaction t ON t.id = tal.transaction
INNER JOIN account a ON a.id = tal.account
INNER JOIN accountingperiod ap ON ap.id = t.postingperiod
WHERE tal.posting = 'T'
  AND tal.accountingbook = (SELECT id FROM accountingbook WHERE isprimary = 'T')
  AND ap.periodname = 'Jan 2026'
  AND a.accttype IN ('Income', 'COGS', 'Expense', 'OthIncome', 'OthExpense')
GROUP BY a.acctnumber, BUILTIN.DF(a.accttype)
ORDER BY a.acctnumber
```

**Balance Sheet consolidation example (uses current/period-end exchange rate):**
```sql
SELECT
    a.acctnumber,
    BUILTIN.DF(a.accttype) AS account_type,
    SUM(BUILTIN.CONSOLIDATE(tal.amount, 'LEDGER', 'DEFAULT', 'DEFAULT', 1, ap.id, 'DEFAULT')
        * CASE WHEN a.accttype IN ('AcctPay','CreditCard','OthCurrLiab','LongTermLiab','DeferRevenue','Equity') THEN -1 ELSE 1 END
    ) AS consolidated_balance
FROM transactionaccountingline tal
INNER JOIN transaction t ON t.id = tal.transaction
INNER JOIN account a ON a.id = tal.account
INNER JOIN accountingperiod ap ON ap.id = t.postingperiod
WHERE tal.posting = 'T'
  AND tal.accountingbook = (SELECT id FROM accountingbook WHERE isprimary = 'T')
  AND ap.enddate <= TO_DATE('2026-01-31', 'YYYY-MM-DD')
  AND ap.isquarter = 'F' AND ap.isyear = 'F'
  AND a.accttype IN ('Bank','AcctRec','UnbilledRec','OthCurrAsset','FixedAsset','OthAsset','DeferExpense',
                      'AcctPay','CreditCard','OthCurrLiab','LongTermLiab','DeferRevenue','Equity')
GROUP BY a.acctnumber, BUILTIN.DF(a.accttype)
ORDER BY a.acctnumber
```

### Exchange Rate Types

NetSuite uses three rate types for currency translation (stored in `consolidatedExchangeRate` table):

| Rate Type | Used For | Applied To |
|-----------|----------|------------|
| **Current Rate** | Period-end spot rate | Balance Sheet: assets, liabilities |
| **Average Rate** | Average over the period | Income Statement: revenue, expenses |
| **Historical Rate** | Rate at time of transaction | Equity accounts, fixed assets |

`BUILTIN.CONSOLIDATE()` automatically picks the correct rate type based on the `view_type` parameter and account type. `'INCOME'` applies average rate; `'LEDGER'` applies current rate for most accounts, historical for equity.

**Without `BUILTIN.CONSOLIDATE()`**: Raw `SUM(tal.amount)` across subsidiaries with different base currencies adds USD + EUR amounts together — producing incorrect totals. Always use CONSOLIDATE for multi-subsidiary reports.

For subsidiary-level breakdown without consolidation:
```sql
SELECT BUILTIN.DF(t.subsidiary) AS subsidiary_name, ...
GROUP BY BUILTIN.DF(t.subsidiary), a.acctnumber, ...
```

## Critical Gotchas for Financial Queries

1. **Balance Sheet = no start date** — only filter by end date (`ap.enddate <=`). Adding a start date is the #1 balance sheet error.
2. **Revenue is negative** — `tal.amount` is negative for Income accounts. Always multiply by `-1`.
3. **Multi-book duplication** — without `tal.accountingbook` filter, totals are 2-3x actual. ALWAYS filter to primary book.
4. **`tal.posting = 'T'`** — only posted transactions appear in native reports. Always include this filter.
5. **Use `t.postingperiod`** — join `AccountingPeriod ap ON ap.id = t.postingperiod`. Do NOT filter by `t.trandate` for formal financial reports — a transaction dated Jan 31 may post to the Feb period.
6. **Period rollup rows** — `accountingperiod` has quarter/year rows. Filter `isquarter='F' AND isyear='F'`.
7. **Elimination accounts** — add `COALESCE(a.eliminate, 'F') = 'F'` to exclude intercompany eliminations.
8. **Statistical accounts** — `accttype = 'Statistical'` are non-monetary. Always exclude from financials.
9. **Multi-subsidiary** — use `BUILTIN.CONSOLIDATE()` for consolidated reports. Without it, multi-currency amounts are mixed.
10. **`LIMIT` not supported** — use `FETCH FIRST N ROWS ONLY`.
