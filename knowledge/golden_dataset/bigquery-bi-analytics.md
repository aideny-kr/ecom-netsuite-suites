---
topic_tags: ["bigquery", "bi", "analytics", "metrics", "kpi"]
source_type: expert_rules
---

# BigQuery BI Analytics Patterns

## Revenue Analysis

### Monthly Revenue Trend

```sql
SELECT
  DATE_TRUNC(order_date, MONTH) AS month,
  ROUND(SUM(net_amount), 2) AS revenue,
  COUNT(*) AS order_count,
  ROUND(SAFE_DIVIDE(SUM(net_amount), COUNT(*)), 2) AS avg_order_value
FROM `frameworkreporting.sales-orders_cleaned`
WHERE order_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 12 MONTH)
  AND orderstatus NOT IN ('Cancelled', 'Voided', 'Closed')
GROUP BY 1
ORDER BY 1
```

### Year-over-Year Comparison

```sql
WITH monthly AS (
  SELECT
    EXTRACT(YEAR FROM order_date) AS year,
    EXTRACT(MONTH FROM order_date) AS month_num,
    FORMAT_DATE('%b', order_date) AS month_name,
    ROUND(SUM(net_amount), 2) AS revenue
  FROM `frameworkreporting.sales-orders_cleaned`
  WHERE orderstatus NOT IN ('Cancelled', 'Voided', 'Closed')
  GROUP BY 1, 2, 3
)
SELECT
  month_name,
  MAX(IF(year = EXTRACT(YEAR FROM CURRENT_DATE()) - 1, revenue, NULL)) AS last_year,
  MAX(IF(year = EXTRACT(YEAR FROM CURRENT_DATE()), revenue, NULL)) AS this_year,
  SAFE_DIVIDE(
    MAX(IF(year = EXTRACT(YEAR FROM CURRENT_DATE()), revenue, NULL)) -
    MAX(IF(year = EXTRACT(YEAR FROM CURRENT_DATE()) - 1, revenue, NULL)),
    MAX(IF(year = EXTRACT(YEAR FROM CURRENT_DATE()) - 1, revenue, NULL))
  ) AS yoy_growth
FROM monthly
GROUP BY month_name, month_num
ORDER BY month_num
```

## Customer Analytics

### Customer Cohort Retention

```sql
WITH first_purchase AS (
  SELECT
    customer_id,
    DATE_TRUNC(MIN(order_date), MONTH) AS cohort_month
  FROM `frameworkreporting.sales-orders_cleaned`
  WHERE orderstatus NOT IN ('Cancelled', 'Voided', 'Closed')
  GROUP BY 1
),
monthly_activity AS (
  SELECT
    s.customer_id,
    fp.cohort_month,
    DATE_TRUNC(s.order_date, MONTH) AS activity_month,
    DATE_DIFF(DATE_TRUNC(s.order_date, MONTH), fp.cohort_month, MONTH) AS months_since_first
  FROM `frameworkreporting.sales-orders_cleaned` s
  JOIN first_purchase fp ON s.customer_id = fp.customer_id
  WHERE s.orderstatus NOT IN ('Cancelled', 'Voided', 'Closed')
)
SELECT
  cohort_month,
  months_since_first,
  COUNT(DISTINCT customer_id) AS active_customers,
  SAFE_DIVIDE(
    COUNT(DISTINCT customer_id),
    FIRST_VALUE(COUNT(DISTINCT customer_id)) OVER (
      PARTITION BY cohort_month ORDER BY months_since_first
    )
  ) AS retention_rate
FROM monthly_activity
GROUP BY 1, 2
ORDER BY 1, 2
```

### Customer Lifetime Value (LTV)

```sql
SELECT
  customer_id,
  COUNT(*) AS total_orders,
  ROUND(SUM(net_amount), 2) AS lifetime_revenue,
  MIN(order_date) AS first_order,
  MAX(order_date) AS last_order,
  DATE_DIFF(MAX(order_date), MIN(order_date), DAY) AS customer_tenure_days,
  ROUND(SAFE_DIVIDE(SUM(net_amount), COUNT(*)), 2) AS avg_order_value
FROM `frameworkreporting.sales-orders_cleaned`
WHERE orderstatus NOT IN ('Cancelled', 'Voided', 'Closed')
GROUP BY 1
ORDER BY lifetime_revenue DESC
LIMIT 100
```

### ARPU (Average Revenue Per User)

```sql
SELECT
  DATE_TRUNC(order_date, MONTH) AS month,
  COUNT(DISTINCT customer_id) AS unique_customers,
  ROUND(SUM(net_amount), 2) AS total_revenue,
  ROUND(SAFE_DIVIDE(SUM(net_amount), COUNT(DISTINCT customer_id)), 2) AS arpu
FROM `frameworkreporting.sales-orders_cleaned`
WHERE orderstatus NOT IN ('Cancelled', 'Voided', 'Closed')
  AND order_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 12 MONTH)
GROUP BY 1
ORDER BY 1
```

## Growth and Trend Detection

### Month-over-Month Growth Rate

```sql
WITH monthly AS (
  SELECT
    DATE_TRUNC(order_date, MONTH) AS month,
    ROUND(SUM(net_amount), 2) AS revenue
  FROM `frameworkreporting.sales-orders_cleaned`
  WHERE orderstatus NOT IN ('Cancelled', 'Voided', 'Closed')
  GROUP BY 1
)
SELECT
  month,
  revenue,
  LAG(revenue) OVER (ORDER BY month) AS prev_month_revenue,
  ROUND(SAFE_DIVIDE(
    revenue - LAG(revenue) OVER (ORDER BY month),
    LAG(revenue) OVER (ORDER BY month)
  ) * 100, 1) AS mom_growth_pct
FROM monthly
ORDER BY month
```

### Detecting Anomalies (Z-Score Method)

```sql
WITH daily AS (
  SELECT
    order_date,
    SUM(net_amount) AS daily_revenue
  FROM `frameworkreporting.sales-orders_cleaned`
  WHERE order_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
  GROUP BY 1
),
stats AS (
  SELECT
    AVG(daily_revenue) AS mean_rev,
    STDDEV(daily_revenue) AS stddev_rev
  FROM daily
)
SELECT
  d.order_date,
  d.daily_revenue,
  ROUND(SAFE_DIVIDE(d.daily_revenue - s.mean_rev, s.stddev_rev), 2) AS z_score,
  CASE
    WHEN ABS(SAFE_DIVIDE(d.daily_revenue - s.mean_rev, s.stddev_rev)) > 2 THEN 'ANOMALY'
    ELSE 'NORMAL'
  END AS status
FROM daily d
CROSS JOIN stats s
ORDER BY d.order_date
```

## Distribution Analysis

### Order Value Distribution (Bucketing)

```sql
SELECT
  CASE
    WHEN net_amount < 100 THEN 'Under $100'
    WHEN net_amount < 500 THEN '$100-$499'
    WHEN net_amount < 1000 THEN '$500-$999'
    WHEN net_amount < 5000 THEN '$1,000-$4,999'
    ELSE '$5,000+'
  END AS value_bucket,
  COUNT(*) AS order_count,
  ROUND(SUM(net_amount), 2) AS bucket_revenue,
  ROUND(SAFE_DIVIDE(COUNT(*), SUM(COUNT(*)) OVER ()) * 100, 1) AS pct_of_orders
FROM `frameworkreporting.sales-orders_cleaned`
WHERE orderstatus NOT IN ('Cancelled', 'Voided', 'Closed')
  AND order_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 12 MONTH)
GROUP BY 1
ORDER BY MIN(net_amount)
```

### Percentile Analysis

```sql
SELECT
  APPROX_QUANTILES(net_amount, 100)[OFFSET(25)] AS p25,
  APPROX_QUANTILES(net_amount, 100)[OFFSET(50)] AS median,
  APPROX_QUANTILES(net_amount, 100)[OFFSET(75)] AS p75,
  APPROX_QUANTILES(net_amount, 100)[OFFSET(90)] AS p90,
  APPROX_QUANTILES(net_amount, 100)[OFFSET(99)] AS p99
FROM `frameworkreporting.sales-orders_cleaned`
WHERE orderstatus NOT IN ('Cancelled', 'Voided', 'Closed')
  AND order_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 12 MONTH)
```

## Pivot and Cross-Tabulation

### Revenue by Product × Month (Pivot)

```sql
SELECT
  product_category,
  SUM(IF(DATE_TRUNC(order_date, MONTH) = '2026-01-01', net_amount, 0)) AS jan_2026,
  SUM(IF(DATE_TRUNC(order_date, MONTH) = '2026-02-01', net_amount, 0)) AS feb_2026,
  SUM(IF(DATE_TRUNC(order_date, MONTH) = '2026-03-01', net_amount, 0)) AS mar_2026
FROM `frameworkreporting.sales-orders_cleaned`
WHERE orderstatus NOT IN ('Cancelled', 'Voided', 'Closed')
  AND order_date >= '2026-01-01'
GROUP BY 1
ORDER BY 1
```

For dynamic pivots with unknown column values, use `netsuite_pivot_query_result` tool — it handles the pivoting server-side.

## Key BI Metrics to Know

| Metric | Formula | Why It Matters |
|--------|---------|----------------|
| **AOV** (Average Order Value) | `SUM(revenue) / COUNT(orders)` | Pricing effectiveness |
| **ARPU** | `SUM(revenue) / COUNT(DISTINCT customers)` | Customer monetization |
| **Retention Rate** | `Returning customers / Total customers from cohort` | Product-market fit |
| **Churn Rate** | `1 - Retention Rate` | Customer loss velocity |
| **MoM Growth** | `(This month - Last month) / Last month` | Growth trajectory |
| **LTV** | `ARPU × Average customer lifespan` | Customer acquisition budget |
| **CAC Payback** | `CAC / (ARPU × Gross Margin)` | Unit economics |
| **Gross Margin** | `(Revenue - COGS) / Revenue` | Business health |

## Common Mistakes in BigQuery BI Queries

1. **Forgetting to exclude cancelled orders** — always filter `orderstatus NOT IN ('Cancelled', 'Voided', 'Closed')`
2. **Using COUNT(*) instead of COUNT(DISTINCT)** for customer counts — duplicates inflate metrics
3. **Not handling NULL in divisions** — always use `SAFE_DIVIDE()`, never bare `/`
4. **Scanning full table** — always add date filters on partitioned columns
5. **Mixing currencies without conversion** — check if amounts are in transaction or base currency
6. **Using LIMIT without ORDER BY** — results are non-deterministic
7. **Forgetting ROUND()** — raw floats like 1234.5678901 are unreadable
