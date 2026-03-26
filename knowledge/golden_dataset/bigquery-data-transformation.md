---
topic_tags: ["bigquery", "data-transformation", "cleaning", "deduplication", "etl"]
source_type: expert_rules
---

# BigQuery Data Transformation Patterns

## Data Cleaning

### Deduplication

```sql
-- Remove duplicates by keeping the most recent row per key
WITH ranked AS (
  SELECT *,
    ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY updated_at DESC) AS rn
  FROM `frameworkreporting.sales-orders`
)
SELECT * EXCEPT(rn)
FROM ranked
WHERE rn = 1
```

### NULL Handling

```sql
-- Coalesce multiple fallback columns
SELECT
  order_id,
  COALESCE(ship_date, estimated_ship_date, order_date) AS effective_ship_date,
  IFNULL(discount_amount, 0) AS discount_amount,
  IF(customer_name IS NULL OR customer_name = '', 'Unknown', customer_name) AS customer_name
FROM orders
```

### Type Casting

```sql
-- String to date
PARSE_DATE('%Y-%m-%d', date_string)
SAFE.PARSE_DATE('%m/%d/%Y', us_format_date)  -- SAFE prefix returns NULL on failure

-- String to number (safe)
SAFE_CAST(amount_string AS FLOAT64)
SAFE_CAST(quantity_string AS INT64)

-- Timestamp to date
DATE(created_timestamp)
DATE(created_timestamp, 'America/Los_Angeles')  -- timezone-aware
```

## Data Transformation

### Unpivoting (Columns → Rows)

```sql
-- Turn monthly columns into rows for time-series analysis
SELECT order_id, 'jan' AS month, jan_amount AS amount FROM monthly_data
UNION ALL
SELECT order_id, 'feb', feb_amount FROM monthly_data
UNION ALL
SELECT order_id, 'mar', mar_amount FROM monthly_data

-- Better: UNPIVOT syntax (BigQuery Standard SQL)
SELECT *
FROM monthly_data
UNPIVOT(amount FOR month IN (jan_amount AS 'jan', feb_amount AS 'feb', mar_amount AS 'mar'))
```

### Pivoting (Rows → Columns)

```sql
-- Turn category rows into columns
SELECT
  order_date,
  SUM(IF(category = 'Laptops', amount, 0)) AS laptops,
  SUM(IF(category = 'Accessories', amount, 0)) AS accessories,
  SUM(IF(category = 'Parts', amount, 0)) AS parts
FROM order_lines
GROUP BY 1
```

For dynamic pivots where column values aren't known in advance, use the `netsuite_pivot_query_result` tool which handles this deterministically.

### Flattening Nested/Repeated Fields

```sql
-- BigQuery supports nested STRUCT and ARRAY types
SELECT
  order_id,
  item.product_name,
  item.quantity,
  item.price
FROM orders,
UNNEST(line_items) AS item
WHERE item.quantity > 0
```

## Cross-System Data Joining

### BigQuery ↔ NetSuite Reconciliation

When comparing BigQuery data (warehouse) with NetSuite data (ERP):

1. **Match keys**: BigQuery `order_id` often maps to NetSuite `tranid` (with prefix)
2. **Currency**: Ensure both sides use the same currency (base or transaction)
3. **Date alignment**: BigQuery may use UTC, NetSuite uses subsidiary timezone
4. **Status mapping**: BigQuery status strings (e.g., 'Completed') differ from NetSuite single-letter codes ('G')

```sql
-- Example: Find orders in BigQuery missing from NetSuite
SELECT
  bq.order_id,
  bq.order_date,
  bq.total_amount
FROM `frameworkreporting.sales-orders_cleaned` bq
LEFT JOIN `frameworkreporting.netsuite_items` ns
  ON bq.order_id = ns.external_id
WHERE ns.external_id IS NULL
  AND bq.order_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
```

## Incremental Processing Patterns

### Watermark-Based Incremental

```sql
-- Only process records updated since last run
SELECT *
FROM `frameworkreporting.sales-orders`
WHERE updated_at > TIMESTAMP('2026-03-24 00:00:00 UTC')
  AND updated_at <= CURRENT_TIMESTAMP()
```

### Merge (Upsert) Pattern

```sql
-- BigQuery MERGE for upsert operations
MERGE INTO `target_table` T
USING `staging_table` S
ON T.order_id = S.order_id
WHEN MATCHED THEN
  UPDATE SET T.status = S.status, T.updated_at = S.updated_at
WHEN NOT MATCHED THEN
  INSERT (order_id, status, created_at, updated_at)
  VALUES (S.order_id, S.status, S.created_at, S.updated_at)
```

Note: MERGE is a write operation — the BI agent is read-only and cannot execute MERGE. This pattern is documented for understanding ETL pipelines, not for direct execution.

## Data Quality Checks

```sql
-- NULL rate per column
SELECT
  ROUND(SAFE_DIVIDE(COUNTIF(customer_id IS NULL), COUNT(*)) * 100, 1) AS null_customer_pct,
  ROUND(SAFE_DIVIDE(COUNTIF(order_date IS NULL), COUNT(*)) * 100, 1) AS null_date_pct,
  ROUND(SAFE_DIVIDE(COUNTIF(net_amount IS NULL OR net_amount = 0), COUNT(*)) * 100, 1) AS null_amount_pct
FROM `frameworkreporting.sales-orders_cleaned`

-- Duplicate detection
SELECT order_id, COUNT(*) AS dupes
FROM `frameworkreporting.sales-orders`
GROUP BY 1
HAVING COUNT(*) > 1
ORDER BY dupes DESC
LIMIT 20

-- Referential integrity check
SELECT COUNT(*) AS orphaned_lines
FROM order_lines ol
LEFT JOIN orders o ON ol.order_id = o.order_id
WHERE o.order_id IS NULL
```
