---
Name: Sales by Platform Analysis
Description: Breaks down sales orders by product platform (e.g., Dogwood, Tulip, Lilac) with order counts and revenue totals in USD.
Triggers:
  - /sales-by-platform
  - sales by platform
  - platform breakdown
  - revenue by platform
---

# Sales by Platform Analysis

You are executing the Sales by Platform Analysis skill. Follow these exact steps:

1. **Determine Date Range:**
   - Check if the user specified a date range (e.g., "this month", "Q1 2026", "during CES").
   - If no date range specified, default to the current month: `t.trandate >= TRUNC(SYSDATE, 'MM')`.

2. **Run the Query:**
   - Execute this SuiteQL pattern via `netsuite_suiteql`:
   ```sql
   SELECT BUILTIN.DF(i.custitem_fw_platform) as platform,
          COUNT(DISTINCT t.id) as order_count,
          ROUND(SUM(tl.amount * -1), 2) as revenue_usd
   FROM transactionline tl
   JOIN transaction t ON tl.transaction = t.id
   JOIN item i ON i.id = tl.item
   WHERE t.type = 'SalesOrd'
     AND t.trandate >= <start_date>
     AND t.trandate <= <end_date>
     AND tl.mainline = 'F'
     AND tl.taxline = 'F'
     AND (tl.iscogs = 'F' OR tl.iscogs IS NULL)
   GROUP BY BUILTIN.DF(i.custitem_fw_platform)
   ORDER BY revenue_usd DESC
   ```

3. **Present Results:**
   - Format as a markdown table with columns: Platform, Order Count, Revenue (USD).
   - Include a total row at the bottom.
   - Highlight the top-performing platform.
