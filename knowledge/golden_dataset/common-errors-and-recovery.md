---
topic_tags: ["suiteql", "errors", "debugging"]
source_type: expert_rules
---

# Common Errors and Recovery

## "Unknown identifier" Error

The query references a column that doesn't exist on the table.

**Recovery:**
1. Check for typos in column names
2. Run `SELECT * FROM <table> WHERE ROWNUM <= 1` to discover actual column names
3. Check if the field is a custom field that exists on a different record type
4. Call `netsuite_get_metadata` for the record type

```sql
-- Discover actual columns on a table
SELECT * FROM transaction WHERE ROWNUM <= 1
```

## "Invalid or unsupported search" Error

The record type is not accessible via the current API path.

**Recovery:**
- Switch from external MCP (`ns_runCustomSuiteQL`) to local REST API (`netsuite_suiteql`) which has full permissions
- The local REST API supports ALL tables including `customrecord_*`

## Zero Rows Returned

Query ran successfully but found no matching data.

**Diagnosis checklist:**
1. Check date functions — is `TRUNC(SYSDATE)` returning the expected date? (Server time may differ from user's timezone)
2. Check type filter — is the type code correct? (e.g., `SalesOrd` not `SalesOrder`)
3. Check if the data simply doesn't exist for the filter criteria
4. For 0 rows on core tables (transaction, customer, item), the tool may inject a `permission_warning` — check for it and stop retrying

**Do NOT assume permissions are wrong** for 0-row results. It's often a legitimate result.

## Syntax Errors

Common SuiteQL syntax mistakes:

| Mistake | Fix |
|---------|-----|
| `LIMIT 10` | `FETCH FIRST 10 ROWS ONLY` |
| `CURRENT_DATE` | `TRUNC(SYSDATE)` |
| `NOW()` | `SYSDATE` |
| `internalid` | `id` |
| `BUILTIN.DATE(SYSDATE)` | `TRUNC(SYSDATE)` |
| Missing quotes around type | `t.type = 'SalesOrd'` (with quotes) |

## Large Result Set Timeout

If a query returns hundreds of rows and the response is slow:

**The problem:** Fetching all individual rows wastes tokens and can time out.

**The fix:** Use GROUP BY with aggregate functions to get summaries instead:
```sql
-- BAD: Fetching all 257 individual rows
SELECT t.id, t.tranid, t.foreigntotal FROM transaction t WHERE t.type = 'SalesOrd'

-- GOOD: Aggregate summary
SELECT COUNT(*) as order_count, SUM(t.foreigntotal) as total
FROM transaction t WHERE t.type = 'SalesOrd' AND t.trandate = TRUNC(SYSDATE)
```

## Error Recovery Protocol

1. First failure: Fix the specific error (column name, syntax, etc.) and retry
2. Second failure: Try a different approach (different table, different tool)
3. Third failure: Report what went wrong with the queries you tried
4. Each retry MUST be meaningfully different from the previous attempt
5. NEVER retry the exact same query that just failed
