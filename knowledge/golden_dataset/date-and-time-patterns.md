---
topic_tags: ["suiteql", "dates", "time"]
source_type: expert_rules
---

# Date and Time Patterns

## Core Date Functions

SuiteQL uses Oracle-style date functions:

| Expression | Result |
|-----------|--------|
| `SYSDATE` | Current date and time (server time) |
| `TRUNC(SYSDATE)` | Today at midnight (date only) |
| `TRUNC(SYSDATE) - 1` | Yesterday |
| `TRUNC(SYSDATE) - 7` | 7 days ago |
| `TRUNC(SYSDATE) - 30` | 30 days ago |
| `TO_DATE('2026-01-15', 'YYYY-MM-DD')` | Specific date |

## Common Date Patterns

### Today
```sql
WHERE t.trandate = TRUNC(SYSDATE)
```

### Yesterday
```sql
WHERE t.trandate = TRUNC(SYSDATE) - 1
```

### Date Range (Last N Days)
```sql
WHERE t.trandate >= TRUNC(SYSDATE) - 7
  AND t.trandate <= TRUNC(SYSDATE)
```

### Specific Date Range
```sql
WHERE t.trandate >= TO_DATE('2026-01-01', 'YYYY-MM-DD')
  AND t.trandate <= TO_DATE('2026-01-31', 'YYYY-MM-DD')
```

### This Month
```sql
WHERE t.trandate >= TRUNC(SYSDATE, 'MM')
```

### Last Month
```sql
WHERE t.trandate >= ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -1)
  AND t.trandate < TRUNC(SYSDATE, 'MM')
```

### This Year
```sql
WHERE t.trandate >= TRUNC(SYSDATE, 'YYYY')
```

## Period Boundaries

Use `TRUNC` with format masks for period boundaries:

```sql
TRUNC(SYSDATE, 'MM')   -- First day of current month
TRUNC(SYSDATE, 'YYYY') -- First day of current year
TRUNC(SYSDATE, 'Q')    -- First day of current quarter
```

## Date Arithmetic

```sql
-- Add/subtract days
TRUNC(SYSDATE) + 1         -- Tomorrow
TRUNC(SYSDATE) - 30        -- 30 days ago

-- Add/subtract months
ADD_MONTHS(SYSDATE, 1)     -- One month from now
ADD_MONTHS(SYSDATE, -3)    -- Three months ago

-- Difference in days between dates
t.trandate - TRUNC(SYSDATE) as days_ago
```

## Grouping by Time Period

```sql
-- By month
SELECT TO_CHAR(t.trandate, 'YYYY-MM') as month,
       COUNT(*) as orders,
       SUM(t.total) as total
FROM transaction t
WHERE t.type = 'SalesOrd'
  AND t.trandate >= ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -6)
GROUP BY TO_CHAR(t.trandate, 'YYYY-MM')
ORDER BY month

-- By week
SELECT TO_CHAR(t.trandate, 'IYYY-IW') as week,
       COUNT(*) as orders
FROM transaction t
WHERE t.type = 'SalesOrd'
  AND t.trandate >= TRUNC(SYSDATE) - 28
GROUP BY TO_CHAR(t.trandate, 'IYYY-IW')
ORDER BY week
```

## Timezone Considerations — CRITICAL for Matching Saved Searches

`SYSDATE` and `TRUNC(SYSDATE)` return the NetSuite **server's** time (Pacific Time), which may differ from the user's local timezone and from the company/subsidiary timezone configured in NetSuite. This causes daily totals to disagree with saved searches because the "day boundary" is sliced at different hours.

**Why this matters:** A saved search for "Today's Sales" uses the company timezone to determine what "today" means. SuiteQL with `WHERE trandate = TRUNC(SYSDATE)` uses server time. If the company is in EST and the server is in PST, there's a 3-hour window where "today" means different dates.

### Option A: `BUILTIN.RELATIVE_RANGES` (Safest — matches saved search behavior)

NetSuite's built-in relative date functions respect the user/company timezone settings:

```sql
-- Today (timezone-aware, matches saved search "Today")
WHERE t.trandate = BUILTIN.RELATIVE_RANGES('TODAY', 'START')

-- This month
WHERE t.trandate >= BUILTIN.RELATIVE_RANGES('THIS_MONTH', 'START')
  AND t.trandate <= BUILTIN.RELATIVE_RANGES('THIS_MONTH', 'END')

-- Last month
WHERE t.trandate >= BUILTIN.RELATIVE_RANGES('LAST_MONTH', 'START')
  AND t.trandate <= BUILTIN.RELATIVE_RANGES('LAST_MONTH', 'END')

-- This year
WHERE t.trandate >= BUILTIN.RELATIVE_RANGES('THIS_YEAR', 'START')
```

**Use `BUILTIN.RELATIVE_RANGES` whenever comparing against saved searches.** This is the ONLY way to guarantee your SuiteQL date boundaries match the NetSuite UI.

### Option B: Explicit `TO_DATE` with user-provided dates

When the user provides specific dates or the agent knows the user's timezone:

```sql
-- Specific date range (timezone-neutral — safe for multi-day ranges)
WHERE t.trandate >= TO_DATE('2026-01-01', 'YYYY-MM-DD')
  AND t.trandate <= TO_DATE('2026-01-31', 'YYYY-MM-DD')
```

This is fine for multi-day ranges but can still be off by one day at the boundaries for single-day queries.

### Option C: `TRUNC(SYSDATE)` (Use with caution)

Only use `TRUNC(SYSDATE)` for approximate/relative queries where exact timezone alignment with saved searches is not required. For "show me this week's trends" it's fine. For "why doesn't my daily total match the saved search" it's not.

### When the user says "today" — decision tree

1. If comparing against a saved search → use `BUILTIN.RELATIVE_RANGES('TODAY', 'START')`
2. If the user's timezone is known (via X-Timezone header) → use `TO_DATE('user-local-date', 'YYYY-MM-DD')`
3. Fallback → use `TRUNC(SYSDATE)` but warn the user about potential timezone offset

## Functions That Do NOT Work

- `BUILTIN.DATE(SYSDATE)` — does not work for date comparisons, returns 0 rows
- `CURRENT_DATE` — not reliably supported
- `NOW()` — not supported in SuiteQL
- `DATE()` — not a valid SuiteQL function
- `GETDATE()` — SQL Server syntax, not supported
