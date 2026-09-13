---
Name: Period-over-Period Comparison
Description: Compares scoped sales or financial metrics between two time periods with deterministic changes and explicit currency and period bases.
Triggers:
  - /period-compare
  - compare periods
  - month over month
  - year over year
  - compare sales
---

# Period-over-Period Comparison

1. **Determine scope and periods:**
   - Respect the user's selected data source; use its available tools and dialect. Do not switch a Metabase analysis to NetSuite.
   - Use the requested periods. If one is specified, use the immediately preceding equivalent period as a stated assumption; if neither is specified, state current month versus previous month. Identify partial-period comparisons.
   - For accounting measures, verify the source fiscal calendar and actual posting-period dates rather than inferring them from month names or an application default. Reuse current reference evidence within the task.

2. **Define a consistent measure:**
   - Never label sales-order totals as recognized revenue. For a named financial metric, use the metric catalog's definition; GL measures require posted accounts, book and reporting currency.
   - Apply identical inclusion rules, subsidiary scope, amount-field basis and signs to both periods. Verify fields on the selected connector before constructing SQL.
   - Keep transaction, subsidiary base and consolidated currencies distinct. Compare each currency separately unless a verified reporting/conversion basis supports a common currency; do not assume subsidiary base amounts are USD.
   - Aggregate order counts at header grain, or use COUNT(DISTINCT order ID) when line joins are necessary. Never sum repeated header totals. Preserve credit/debit signs and net credits appropriately.

3. **Compute and present:**
   - Compute current, prior, change and percentage change in SQL or an available deterministic calculation tool using the same metric definition. Do not improvise arithmetic in the narrative.
   - If the prior value is zero, report percentage change as undefined; do not divide by zero. Missing/unavailable data is not a zero value. Explain negative-denominator or partial-period comparisons where they affect interpretation.
   - Present Metric, Currency/Reporting Basis, Prior Period, Current Period, Change and % Change. State the actual dates and whether either period is incomplete; qualify role visibility and any truncated results.
