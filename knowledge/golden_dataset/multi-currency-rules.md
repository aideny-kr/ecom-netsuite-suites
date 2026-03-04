---
topic_tags: ["suiteql", "currency", "multi-currency"]
source_type: expert_rules
---

# Multi-Currency Rules

## Currency Fields Overview

NetSuite stores amounts in two currencies for every transaction:

| Field | Currency | Level | Description |
|-------|----------|-------|-------------|
| `t.foreigntotal` | Transaction currency | Header | Total in the currency the order was placed in (USD, EUR, GBP, etc.) |
| `t.total` | Base currency | Header | Total converted to subsidiary's base currency (usually USD) |
| `tl.foreignamount` | Transaction currency | Line | Line amount in transaction currency |
| `tl.amount` | Base currency | Line | Line amount in subsidiary base currency |
| `t.currency` | — | Header | Currency record ID (use `BUILTIN.DF(t.currency)` for name) |
| `t.exchangerate` | — | Header | Conversion rate from transaction currency to base currency |

## When User Asks for "Total in USD"

Use `SUM(t.total)` — this is already converted to the subsidiary's base currency (USD for US-based companies). No manual conversion needed:

```sql
SELECT COUNT(*) as order_count,
       SUM(t.total) as total_usd
FROM transaction t
WHERE t.type = 'SalesOrd'
  AND t.trandate = TRUNC(SYSDATE)
```

## When User Asks for Breakdown by Currency

Use `SUM(t.foreigntotal)` grouped by currency:

```sql
SELECT BUILTIN.DF(t.currency) as currency,
       COUNT(*) as order_count,
       SUM(t.foreigntotal) as total_in_currency
FROM transaction t
WHERE t.type = 'SalesOrd'
  AND t.trandate = TRUNC(SYSDATE)
GROUP BY BUILTIN.DF(t.currency)
ORDER BY total_in_currency DESC
```

## Complete Picture: Both USD Total and Currency Breakdown

For a comprehensive answer, provide BOTH:
1. Unified base-currency total: `SUM(t.total)` — single number in USD
2. Per-currency breakdown: `SUM(t.foreigntotal) GROUP BY currency`

## Line-Level Currency Fields

When joining transactionline and aggregating by line amounts:

```sql
-- Base currency (USD) line totals — DEFAULT for revenue
SUM(tl.amount * -1) as revenue_usd

-- Transaction currency line totals — ONLY for per-currency breakdown
SUM(tl.foreignamount * -1) as revenue_in_txn_currency
```

**DEFAULT**: Use `tl.amount` (base currency / USD) for line-level revenue totals. NEVER use `tl.foreignamount` for totals — it mixes different currencies (EUR + USD) and produces inflated, meaningless numbers.

**IMPORTANT**: Always filter non-revenue lines when summing amounts:
```sql
WHERE tl.mainline = 'F' AND tl.taxline = 'F' AND (tl.iscogs = 'F' OR tl.iscogs IS NULL)
```
Note: `tl.iscogs` can be NULL on some lines — always include the `IS NULL` fallback.

## Margin / COGS Calculations — Currency Consistency

When computing margins (revenue minus COGS), ALWAYS use the same currency column for both sides. Use `tl.amount` (base currency) for both revenue and COGS lines. NEVER mix `tl.foreignamount` with `tl.amount` — summing `foreignamount` across currencies (AUD + GBP + EUR) produces meaningless numbers.

```sql
-- CORRECT: both revenue and COGS use tl.amount (base currency)
SUM(tl.amount * -1) as revenue_usd   -- from CustInvc lines
SUM(tl.amount * -1) as cogs_usd      -- from iscogs = 'T' lines

-- WRONG: mixing currency columns
SUM(tl.foreignamount * -1) as revenue   -- transaction currency (mixed!)
SUM(tl.amount * -1) as cogs             -- base currency (USD)
```

## Exchange Rate Considerations

- `t.exchangerate` converts from transaction currency to subsidiary base currency
- Formula: `t.foreigntotal * t.exchangerate = t.total` (approximately)
- For most US-based companies, base currency is USD, so `t.total` is already in USD
- You typically do NOT need to manually multiply by exchange rates
