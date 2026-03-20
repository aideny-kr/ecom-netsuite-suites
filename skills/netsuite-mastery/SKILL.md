---
name: netsuite-mastery
description: >
  Complete reference for NetSuite SuiteQL queries, SuiteScript 2.1 patterns, REST API behaviors,
  and OAuth integration. Use this skill whenever working on SuiteQL queries, SuiteScript development,
  NetSuite REST API calls, File Cabinet operations, or debugging NetSuite-specific issues. Trigger
  on any mention of SuiteQL, SuiteScript, NetSuite, RESTlet, transaction queries, inventory queries,
  custom records, item tables, or NetSuite OAuth. Also trigger when writing agent prompts that need
  NetSuite domain knowledge, or when a query returns unexpected results (0 rows, wrong totals, etc.).
---

# NetSuite SuiteQL & SuiteScript Mastery

This skill encodes hard-won tribal knowledge about NetSuite's quirks, silent failures, and
non-obvious behaviors. Most of these were discovered through production debugging and cannot
be found in Oracle's official documentation. When in doubt, trust this skill over general
NetSuite knowledge — these patterns are verified against live REST API behavior.

## SuiteQL Core Rules

### Pagination — No LIMIT keyword

SuiteQL does not support `LIMIT`. Queries using it will error out silently or return unexpected results.

**Use `FETCH FIRST N ROWS ONLY`** (preferred for "latest N" queries):
```sql
SELECT t.id, t.tranid, t.trandate
FROM transaction t
ORDER BY t.id DESC
FETCH FIRST 10 ROWS ONLY
```

**`ROWNUM` is a trap with ORDER BY.** `WHERE ROWNUM <= 10` evaluates *before* sorting, meaning
you get 10 arbitrary rows, then sorted — not the top 10. Only use ROWNUM for unordered result limiting.

### Status Codes — Single-Letter via REST API

This is the most common source of "query returns 0 rows" bugs. The NetSuite UI and Saved Searches
display compound codes like `SalesOrd:B`, but the REST API normalizes them to single letters.

**Wrong** (silently returns 0 rows):
```sql
WHERE t.status = 'SalesOrd:B'
```

**Correct:**
```sql
WHERE t.status = 'B'
```

Reference table for common transaction types:

| Type | A | B | C | D | E | F | G | H |
|------|---|---|---|---|---|---|---|---|
| Sales Order | Pending Approval | Pending Fulfillment | Cancelled | Partially Fulfilled | Pending Billing/Part. Fulfilled | Pending Billing | Billed | Closed |
| Purchase Order | Pending Supervisor | Pending Receipt | Rejected | Partially Received | Pending Bill/Part. Received | Pending Bill | Fully Billed | Closed |
| Invoice | Open | Paid In Full | — | — | — | — | — | — |

To exclude closed/cancelled orders: `WHERE t.status NOT IN ('C', 'H')`

### Primary Keys and Sorting

- `id` is the sequential primary key (higher = more recent). Prefer `ORDER BY t.id DESC` for "latest" queries — more reliable than date columns.
- `tranid` is the user-facing transaction number (e.g., "SO12345"). Use for display, not sorting.
- `internalid` is not a standard column in SuiteQL — use `id` instead.

### Date Functions — Timezone Awareness

```sql
-- Today (timezone-aware, respects company setting):
BUILTIN.RELATIVE_RANGES('TODAY', 'START')

-- Fallback for today (server time, may differ by hours):
TRUNC(SYSDATE)

-- Yesterday:
TRUNC(SYSDATE) - 1

-- Last 7 days:
WHERE t.trandate >= TRUNC(SYSDATE) - 7

-- Specific date:
WHERE t.trandate = TO_DATE('2026-01-15', 'YYYY-MM-DD')

-- This month:
BUILTIN.RELATIVE_RANGES('THIS_MONTH', 'START')
```

**Never use:** `BUILTIN.DATE(SYSDATE)` (doesn't work for comparisons) or `CURRENT_DATE` (not reliably supported).

## Transaction Queries — Avoiding Double-Counting

### Header vs Line Aggregation Trap

`t.foreigntotal` and `t.total` are header-level fields. When you JOIN `transactionline`, the header
value is duplicated for every line item. `SUM(t.foreigntotal)` in a joined query inflates totals
by the number of line items per order.

**For order-level totals:** Query `transaction` alone, no join to transactionline.
**For line-level breakdown:** Use `SUM(tl.foreignamount)` — the line-level field.

### Transaction Type Double-Counting

A sale flows: Sales Order → Invoice → (optional) Cash Sale. These are different records for the
same underlying sale. Filtering `t.type IN ('SalesOrd', 'CustInvc')` and summing amounts
double-counts revenue.

- **Order volume/revenue analysis:** `t.type = 'SalesOrd'` only
- **Recognized revenue:** `t.type = 'CustInvc'` only (invoices = booked revenue)
- **POS/cash sales:** `t.type = 'CashSale'` only

### Line Amount Sign Convention

`tl.foreignamount` is NEGATIVE for revenue lines on sales orders, invoices, and credit memos
(accounting convention). `t.foreigntotal` (header) is POSITIVE for the same transactions.

When presenting line-level sales totals: negate with `SUM(tl.foreignamount) * -1` or use `ABS()`.

### Multi-Currency Fields

- `t.foreigntotal` = amount in TRANSACTION currency
- `t.total` = amount in SUBSIDIARY BASE currency (usually USD)
- `tl.foreignamount` / `tl.netamount` = line amounts in TRANSACTION currency
- `tl.amount` = line amounts in SUBSIDIARY BASE currency

### Transaction Line Filtering

Always filter out non-item lines when querying line details:
```sql
WHERE tl.mainline = 'F'
  AND tl.taxline = 'F'
  AND (tl.iscogs = 'F' OR tl.iscogs IS NULL)
```

For header-only queries: `WHERE t.mainline = 'T'` or query `transaction` alone.

**Field restrictions via REST API:**
- `tl.itemtype` does NOT work on transactionline (returns 400) — use `i.type` from item table
- `quantityreceived` doesn't exist — correct field is `tl.quantityshiprecv`
- `tl.expectedreceiptdate` exists only on transactionline, not transaction header

## Item Table — The Silent 0-Row Problem

Selecting a non-existent or restricted column on the `item` table causes the entire query to
return 0 rows with no error. Even standard-looking columns like `itemtype`, `class`, `baseprice`,
`salesdescription`, `created`, `lastmodified` can trigger this on certain item types.

**Safe columns (universally available):** `id`, `itemid`, `displayname`, `description`

**Pattern:**
1. Start minimal: `SELECT i.id, i.itemid, i.displayname FROM item i WHERE i.itemid = 'X'`
2. If that returns rows, STOP — don't add more columns
3. If user needs a specific column, run it in a SEPARATE query
4. Adding columns after a successful query will likely cause 0 rows

## Inventory Queries

Use `inventoryitemlocations` table, NOT `inventorybalance` (often restricted via REST API).
`item.quantityavailable` often returns 0 (aggregate behavior unreliable).

```sql
SELECT
    i.itemid,
    i.displayname,
    BUILTIN.DF(iil.location) AS location_name,
    iil.quantityonhand,
    iil.quantityavailable,
    iil.quantitycommitted,
    iil.quantityonorder
FROM inventoryitemlocations iil
JOIN item i ON i.id = iil.item
WHERE i.itemid = '100-500-033'
```

Always break out results BY LOCATION — users expect location-specific data.

## Custom Records

Custom record tables use lowercase scriptid: `customrecord_r_inv_processor`
(NOT `CUSTOMRECORD_R_INV_PROCESSOR`).

Always convert entity mappings to lowercase for queries:
```sql
SELECT * FROM customrecord_r_inv_processor
FETCH FIRST 5 ROWS ONLY
```

## Custom List Fields (SELECT type)

Fields with type SELECT store integer IDs referencing custom lists. To filter:
- Fastest: `WHERE field = <id>`
- Readable: `WHERE BUILTIN.DF(field) = 'Value Name'`

Use `BUILTIN.DF(field)` in SELECT to get display names instead of raw IDs.

## Account ID Normalization

NetSuite account IDs may contain underscores or mixed case. Always normalize:
```python
account_id = raw_id.replace("_", "-").lower()
```
Example: `6738075_SB1` → `6738075-sb1`

## SuiteScript 2.1 Patterns

### RESTlet Envelope Pattern

All RESTlets must return a consistent envelope:
```javascript
return { success: true, data: result, remainingUsage: script.getRemainingUsage() };
// or on error:
return { success: false, error: e.name, message: e.message };
```

Always log with `N/log`, report `remainingUsage` for governance, and wrap in try/catch.

### File Cabinet I/O

NetSuite REST API PATCH for File Cabinet is broken. Use a custom RESTlet instead.
The RESTlet does in-place load → set `.contents` → `.save()` which preserves the file ID.
This is critical — if the file ID changes, all script references break.

### Script Type Detection

Detect from content via `@NScriptType` annotation:
```javascript
const match = content.match(/@NScriptType\s+(\w+)/);
```

Fallback to filename heuristics: `_ue` (UserEvent), `_cs` (Client), `_ss` (Scheduled),
`_mr` (MapReduce), `_su` (Suitelet), `_rl` (Restlet).

## OAuth 2.0 Token Management

- Tokens auto-refresh 60 seconds before expiration
- Token refresh URL: `https://{account_id}.suitetalk.api.netsuite.com/services/rest/auth/oauth2/v1/token`
- OAuth 2.0: `access_token` + `refresh_token`
- OAuth 1.0 (legacy): `consumer_key`, `consumer_secret`, `token_id`, `token_secret`

## Two SuiteQL Execution Paths

1. **Local REST API (`netsuite_suiteql`):** Supports all tables including `customrecord_*`. Use as default.
2. **External MCP (`ns_runCustomSuiteQL`):** Standard tables only. Fallback for OAuth 1.0 or when local tool fails.

Always try the local tool first. Fall back to MCP only when explicitly needed.
