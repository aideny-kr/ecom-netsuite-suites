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

## Timezone Considerations

- `SYSDATE` returns the NetSuite server's time, which may differ from the user's local time
- When the user provides their timezone, use `TO_DATE` with explicit dates instead of `SYSDATE`
- The SuiteQL agent receives the user's local date and should use `TO_DATE('user-local-date', 'YYYY-MM-DD')` instead of `TRUNC(SYSDATE)` when the user says "today"

## Functions That Do NOT Work

- `BUILTIN.DATE(SYSDATE)` — does not work for date comparisons, returns 0 rows
- `CURRENT_DATE` — not reliably supported
- `NOW()` — not supported in SuiteQL
- `DATE()` — not a valid SuiteQL function
- `GETDATE()` — SQL Server syntax, not supported
