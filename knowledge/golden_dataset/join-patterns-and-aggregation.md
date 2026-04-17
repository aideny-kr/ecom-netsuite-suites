---
topic_tags: ["suiteql", "joins", "aggregation"]
source_type: expert_rules
partition_id: netsuite/joins
---

# Join Patterns and Aggregation Rules

## Header vs Line Aggregation (Prevents Double-Counting)

This is the most common source of incorrect results in SuiteQL queries.

`t.foreigntotal` and `t.total` are HEADER-LEVEL fields — they store one value per transaction. When you JOIN `transactionline`, these header values are DUPLICATED for every line item, causing inflated totals.

**The Rule:** If your query has `JOIN transactionline`, you MUST use line-level amount fields. If you need order totals, do NOT join transactionline.

### Correct: Order-Level Totals (No Line Join)

```sql
SELECT COUNT(*) as order_count,
       SUM(t.foreigntotal) as total_sales
FROM transaction t
WHERE t.type = 'SalesOrd'
  AND t.trandate = TRUNC(SYSDATE)
```

### Correct: Line-Level Breakdown

```sql
SELECT BUILTIN.DF(i.displayname) as item_name,
       SUM(tl.foreignamount) * -1 as revenue
FROM transactionline tl
  JOIN transaction t ON tl.transaction = t.id
  JOIN item i ON tl.item = i.id
WHERE t.type = 'SalesOrd'
  AND tl.mainline = 'F' AND tl.taxline = 'F'
GROUP BY BUILTIN.DF(i.displayname)
ORDER BY revenue DESC
FETCH FIRST 20 ROWS ONLY
```

### WRONG: Header Amount with Line Join (Double-Counting)

```sql
-- BAD: SUM(t.foreigntotal) is duplicated per line item!
SELECT SUM(t.foreigntotal) as total
FROM transaction t
  JOIN transactionline tl ON tl.transaction = t.id
WHERE t.type = 'SalesOrd'
-- If an order has 5 line items, foreigntotal is counted 5 times!
```

## Transaction Line Filters

Always filter out non-item lines when joining transactionline:
```sql
WHERE tl.mainline = 'F'    -- Exclude header pseudo-line
  AND tl.taxline = 'F'     -- Exclude tax lines
  AND (tl.iscogs = 'F' OR tl.iscogs IS NULL)  -- Exclude COGS lines (NULL on some line types)
```

### Shipping, Discount, and Subtotal Lines

The standard triple filter above does NOT exclude shipping, discount, or subtotal lines — they pass through all three filters. For strict revenue-only totals, JOIN the item table:

```sql
-- Strict revenue lines only (excludes shipping, discount, subtotal, markup)
SELECT SUM(tl.amount * -1) as revenue_usd
FROM transactionline tl
  JOIN transaction t ON tl.transaction = t.id
  JOIN item i ON tl.item = i.id
WHERE t.type = 'CustInvc' AND t.posting = 'T'
  AND tl.mainline = 'F' AND tl.taxline = 'F'
  AND (tl.iscogs = 'F' OR tl.iscogs IS NULL)
  AND i.type NOT IN ('ShipItem', 'Discount', 'Subtotal', 'Markup', 'Payment', 'EndGroup')
```

**When to use the strict filter:** Comparing against saved searches that show "Item Lines Only" amounts.
**When NOT needed:** General revenue queries where shipping/discount are expected to be included in the total.

For header-only queries without line details, either:
1. Don't join transactionline at all, or
2. Use `WHERE t.mainline = 'T'` on the transaction table

## Transaction Type Double-Counting

A single sale flows through multiple transaction types: Sales Order → Invoice (or Cash Sale). Each is a separate row in `transaction`. NEVER filter `t.type IN ('SalesOrd', 'CustInvc', 'CashSale')` and SUM amounts — this counts the same revenue 2-3x.

**Choose ONE type based on what you're measuring:**
- **Order pipeline** (what was ordered): `t.type = 'SalesOrd'`
- **Recognized revenue** (what was invoiced): `t.type = 'CustInvc'` with `t.posting = 'T'`
- **Cash sales** (POS/immediate): `t.type = 'CashSale'`
- **Payments received**: `t.type = 'CustPymt'`

Saved searches are always scoped to a single record type, which is why they don't have this problem.

## Line Amount Sign Convention

In NetSuite, `tl.foreignamount` is NEGATIVE for revenue lines on sales orders, invoices, and credit memos (accounting convention: credits are negative). The header field `t.foreigntotal` is POSITIVE for the same transactions.

When presenting line-level sales totals, negate to match the positive convention:
```sql
SUM(tl.foreignamount) * -1 as revenue
-- or
ABS(SUM(tl.foreignamount)) as revenue
```

Sort revenue DESC (highest first) for "best sellers" or "top items".

## Aggregation-First Query Strategy

For analytical/summary questions ("total sales", "best seller", "how many", "breakdown by"):
- ALWAYS use GROUP BY and aggregate functions (COUNT, SUM, AVG)
- NEVER fetch all individual rows and try to summarize them — this wastes tokens and can time out
- Keep result sets small: typically < 20 rows for summaries

For multi-part questions (summary + breakdown), use TWO separate aggregation queries:
```sql
-- Query 1: Overall summary
SELECT COUNT(*) as orders, SUM(t.total) as total_usd
FROM transaction t WHERE t.type = 'SalesOrd' AND t.trandate = TRUNC(SYSDATE)

-- Query 2: Breakdown by dimension
SELECT BUILTIN.DF(t.currency) as currency, COUNT(*) as orders, SUM(t.foreigntotal) as total
FROM transaction t WHERE t.type = 'SalesOrd' AND t.trandate = TRUNC(SYSDATE)
GROUP BY BUILTIN.DF(t.currency) ORDER BY total DESC
```

## Worked Example: Sales by Shipping Country

Pattern verified 2026-04-09 (Olivia's country-sales session) and re-validated 2026-04-16 (Phase 1 staging benchmark — 1.00 accuracy, single tool call, $0.069 per query).

### The canonical query

```sql
SELECT
  BUILTIN.DF(sa.country) AS ship_country,
  COUNT(DISTINCT t.id) AS total_orders,
  SUM(ABS(tl.quantity)) AS total_qty,
  ROUND(SUM(tl.amount * -1), 2) AS revenue_usd
FROM transaction t
JOIN transactionShippingAddress sa ON sa.nKey = t.shippingAddress
JOIN transactionline tl ON tl.transaction = t.id
WHERE t.type = 'SalesOrd'
  AND t.status NOT IN ('C', 'H')
  AND sa.country IN ('NO', 'CH', 'NZ', 'SG')
  AND tl.mainline = 'F'
  AND tl.taxline = 'F'
  AND (tl.iscogs = 'F' OR tl.iscogs IS NULL)
  AND tl.assemblycomponent = 'F'
GROUP BY BUILTIN.DF(sa.country)
ORDER BY revenue_usd DESC
FETCH FIRST 100 ROWS ONLY
```

### Critical details

- **Join key is `sa.nKey = t.shippingAddress`** — NOT `sa.recordOwner = t.id`, NOT `sa.transaction = t.id`, NOT `sa.id = t.shippingAddress`. These three wrong keys cause silent 0-row results.
- Use `BUILTIN.DF(sa.country)` for display name ("Switzerland"), `sa.country` for 2-letter ISO code ("CH"). Both work; prefer `BUILTIN.DF` for the output label and raw code for the filter predicate.
- Do NOT use custom body fields (`custbody*_ship_country*`, `custbody*_country*`) for country queries. The standard address join is the source of truth — custbody fields are stale or tenant-specific.
- Single-letter status codes (`'C'`, `'H'`) — never compound codes (`'SalesOrd:C'`), which silently match nothing.
- Standard transactionline revenue filters apply (`mainline='F'`, `taxline='F'`, `iscogs='F' OR NULL`, `assemblycomponent='F'`) to avoid double-counting and exclude tax/COGS/kit-component lines.

### Variations

- **Billing country** (for invoices or tax queries): same pattern but `JOIN transactionBillingAddress ba ON ba.nKey = t.billingAddress`.
- **Recent-launch countries**: add `MIN(t.trandate) AS first_order_date` to the SELECT and `ORDER BY first_order_date DESC` to find newly-launched markets.
- **Cross-tab by month × country**: run the flat GROUP BY above, then use the `pivot_query_result` tool with `rows=month`, `cols=ship_country`, `values=revenue_usd`. Do NOT hand-write CASE WHEN pivot SQL.
