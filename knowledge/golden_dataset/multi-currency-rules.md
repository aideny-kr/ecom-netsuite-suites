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
-- Base currency (USD) line totals — negate for revenue
SUM(tl.amount) * -1 as revenue_usd

-- Transaction currency line totals — negate for revenue
SUM(tl.foreignamount) * -1 as revenue_in_txn_currency
```

## Exchange Rate Considerations

- `t.exchangerate` converts from transaction currency to subsidiary base currency
- Formula: `t.foreigntotal * t.exchangerate = t.total` (approximately)
- For most US-based companies, base currency is USD, so `t.total` is already in USD
- You typically do NOT need to manually multiply by exchange rates
