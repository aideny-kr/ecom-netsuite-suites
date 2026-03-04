---
Name: Period-over-Period Comparison
Description: Compares sales metrics between two time periods (e.g., this month vs last month, Q1 vs Q2) with delta and percentage change.
Triggers:
  - /period-compare
  - compare periods
  - month over month
  - year over year
  - compare sales
---

# Period-over-Period Comparison

You are executing the Period-over-Period Comparison skill. Follow these exact steps:

1. **Determine Periods:**
   - Check if the user specified two periods (e.g., "Jan vs Feb", "Q1 vs Q2 2026").
   - If only one period given, compare it to the immediately preceding period of the same length.
   - If no period specified, compare current month vs previous month.

2. **Run Current Period Query:**
   ```sql
   SELECT COUNT(DISTINCT t.id) as order_count,
          ROUND(SUM(tl.amount * -1), 2) as revenue_usd
   FROM transactionline tl
   JOIN transaction t ON tl.transaction = t.id
   WHERE t.type = 'SalesOrd'
     AND t.trandate >= <current_start>
     AND t.trandate <= <current_end>
     AND tl.mainline = 'F'
     AND tl.taxline = 'F'
     AND (tl.iscogs = 'F' OR tl.iscogs IS NULL)
   ```

3. **Run Prior Period Query:**
   - Same structure with `<prior_start>` and `<prior_end>`.

4. **Calculate & Present:**
   - Compute delta (current - prior) and percentage change ((current - prior) / prior * 100).
   - Present as a comparison table:
     | Metric | Prior Period | Current Period | Change | % Change |
   - Highlight growth in green context, decline in red context.
