---
topic_tags: ["bigquery", "sql", "standard-sql", "dialect"]
source_type: expert_rules
---

# BigQuery Standard SQL Patterns

## Core Syntax Differences from SuiteQL

BigQuery uses Google Standard SQL — NOT Oracle-based SuiteQL. Critical differences:

| Feature | SuiteQL (NetSuite) | BigQuery |
|---------|-------------------|----------|
| Pagination | `FETCH FIRST N ROWS ONLY` | `LIMIT N` |
| Identifiers | Plain names | Backticks: `` `project.dataset.table` `` |
| Date today | `TRUNC(SYSDATE)` | `CURRENT_DATE()` |
| Date truncation | Not supported natively | `DATE_TRUNC(date_col, MONTH)` |
| Safe division | Not available | `SAFE_DIVIDE(a, b)` |
| NULL default | `NVL(col, default)` | `IFNULL(col, default)` or `COALESCE()` |
| Boolean | `'T'` / `'F'` strings | `TRUE` / `FALSE` |
| Display values | `BUILTIN.DF(field)` | Not applicable — values are direct |

## Date and Time Functions

```sql
-- Today
CURRENT_DATE()

-- Truncate to month/quarter/year
DATE_TRUNC(order_date, MONTH)
DATE_TRUNC(order_date, QUARTER)
DATE_TRUNC(order_date, YEAR)

-- Date arithmetic
DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
DATE_ADD(order_date, INTERVAL 30 DAY)
DATE_DIFF(end_date, start_date, DAY)

-- Extract components
EXTRACT(YEAR FROM order_date)
EXTRACT(MONTH FROM order_date)
EXTRACT(DAYOFWEEK FROM order_date)

-- Format for display
FORMAT_DATE('%Y-%m', order_date)
FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', created_at)

-- Parse strings to dates
PARSE_DATE('%Y-%m-%d', date_string)
```

## Aggregation and Grouping

```sql
-- Always use GROUP BY with aggregates — never return raw rows for LLM to sum
SELECT
  DATE_TRUNC(order_date, MONTH) AS month,
  COUNT(*) AS order_count,
  ROUND(SUM(total_amount), 2) AS revenue,
  ROUND(AVG(total_amount), 2) AS avg_order_value
FROM `frameworkreporting.sales-orders_cleaned`
WHERE order_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 12 MONTH)
GROUP BY 1
ORDER BY 1

-- SAFE_DIVIDE prevents division by zero errors
SELECT
  category,
  SAFE_DIVIDE(SUM(returns), SUM(orders)) AS return_rate
FROM sales_summary
GROUP BY 1
```

## Window Functions (Critical for BI)

```sql
-- Month-over-month growth rate
SELECT
  month,
  revenue,
  LAG(revenue) OVER (ORDER BY month) AS prev_month,
  SAFE_DIVIDE(revenue - LAG(revenue) OVER (ORDER BY month),
              LAG(revenue) OVER (ORDER BY month)) AS mom_growth
FROM monthly_revenue

-- Running total / cumulative sum
SELECT
  order_date,
  daily_revenue,
  SUM(daily_revenue) OVER (ORDER BY order_date
    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative_revenue
FROM daily_sales

-- Rank / Top N per group
SELECT *
FROM (
  SELECT
    category,
    product_name,
    revenue,
    ROW_NUMBER() OVER (PARTITION BY category ORDER BY revenue DESC) AS rank
  FROM product_sales
)
WHERE rank <= 5

-- Moving average (7-day)
SELECT
  order_date,
  daily_revenue,
  AVG(daily_revenue) OVER (
    ORDER BY order_date
    ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
  ) AS moving_avg_7d
FROM daily_sales
```

## String and Array Functions

```sql
-- String aggregation
SELECT
  customer_id,
  STRING_AGG(product_name, ', ' ORDER BY order_date) AS products_ordered
FROM order_lines
GROUP BY 1

-- Array handling (flatten before filtering)
SELECT *
FROM table, UNNEST(array_column) AS item
WHERE item = 'target_value'

-- Pattern matching
WHERE REGEXP_CONTAINS(email, r'@gmail\.com$')
WHERE LOWER(product_name) LIKE '%laptop%'
```

## Cost-Aware Querying

BigQuery charges $5 per TB scanned. Always:
- Filter by date range on large tables (partitioned columns)
- Use `SELECT specific_columns` not `SELECT *`
- Use `LIMIT` for exploration queries
- Dry-run with `bigquery_cost_estimate` before large scans
- Use `APPROX_COUNT_DISTINCT()` instead of `COUNT(DISTINCT)` for 10M+ row tables
