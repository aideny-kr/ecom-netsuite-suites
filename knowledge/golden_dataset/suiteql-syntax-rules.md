---
topic_tags: ["suiteql", "syntax", "pagination"]
source_type: expert_rules
---

# SuiteQL Syntax Rules

## Pagination and Row Limiting

SuiteQL uses Oracle-style pagination. The `LIMIT` keyword is NOT supported.

**Correct pagination:**
```sql
SELECT t.id, t.tranid, t.trandate
FROM transaction t
WHERE t.type = 'SalesOrd'
ORDER BY t.id DESC
FETCH FIRST 10 ROWS ONLY
```

**ROWNUM trap:** `ROWNUM` is evaluated BEFORE `ORDER BY`. Using `WHERE ROWNUM <= 10` with `ORDER BY` returns 10 random rows sorted, not the top 10 rows. Only use ROWNUM for unordered limits:
```sql
-- SAFE: no ordering needed
SELECT * FROM customer WHERE ROWNUM <= 100

-- DANGEROUS: returns random 10 rows, then sorts them
SELECT * FROM transaction WHERE ROWNUM <= 10 ORDER BY trandate DESC

-- CORRECT: use FETCH FIRST for ordered results
SELECT * FROM transaction ORDER BY trandate DESC FETCH FIRST 10 ROWS ONLY
```

## Date Functions

SuiteQL uses Oracle date functions, not standard SQL.

**Today and yesterday:**
```sql
-- Today's transactions
WHERE t.trandate = TRUNC(SYSDATE)

-- Yesterday's transactions
WHERE t.trandate = TRUNC(SYSDATE) - 1

-- Last 7 days
WHERE t.trandate >= TRUNC(SYSDATE) - 7

-- Specific date
WHERE t.trandate = TO_DATE('2026-01-15', 'YYYY-MM-DD')
```

**Functions that DO NOT work:**
- `BUILTIN.DATE(SYSDATE)` — returns 0 rows, not a valid date comparison
- `CURRENT_DATE` — not reliably supported in SuiteQL
- `NOW()` — not supported

## Text Display Resolution

Use `BUILTIN.DF(field)` to get display text for List/Record fields:
```sql
SELECT BUILTIN.DF(t.entity) as customer_name,
       BUILTIN.DF(t.status) as status_text,
       BUILTIN.DF(t.currency) as currency_name
FROM transaction t
```

## NVL and CASE

Use Oracle-style `NVL` for null handling:
```sql
SELECT NVL(t.memo, 'No memo') as memo FROM transaction t
```

Use `CASE` for conditional logic:
```sql
SELECT CASE WHEN t.foreigntotal > 1000 THEN 'Large' ELSE 'Small' END as size_category
FROM transaction t
```

## Column Naming Conventions

- Primary key: `id` (NOT `internalid`)
- `id` is sequential — higher id = more recently created
- Transaction date: `trandate`
- Created date: `createddate`
- For "latest" queries, prefer `ORDER BY t.id DESC` — more reliable than date columns
